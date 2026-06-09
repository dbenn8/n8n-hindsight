#!/usr/bin/env node

const path = require("path");
const readline = require("readline");

const packageRoot = path.dirname(require.resolve("n8n-mcp/package.json"));
const { SQLiteStorageService } = require(path.join(
  packageRoot,
  "dist/services/sqlite-storage-service"
));
const { NodeRepository } = require(path.join(
  packageRoot,
  "dist/database/node-repository"
));
const { WorkflowValidator } = require(path.join(
  packageRoot,
  "dist/services/workflow-validator"
));
const { EnhancedConfigValidator } = require(path.join(
  packageRoot,
  "dist/services/enhanced-config-validator"
));

const dbPath = path.join(packageRoot, "data", "nodes.db");

function serializeIssue(issue) {
  return {
    type: issue.type,
    message: issue.message,
    node: issue.nodeName || null,
  };
}

async function createValidator() {
  const storage = new SQLiteStorageService(dbPath);
  const repository = new NodeRepository(storage);
  EnhancedConfigValidator.initializeSimilarityServices(repository);
  return new WorkflowValidator(repository, EnhancedConfigValidator);
}

async function validateWorkflow(validator, workflow) {
  const result = await validator.validateWorkflow(workflow, {
    validateNodes: true,
    validateConnections: true,
    validateExpressions: true,
    profile: "runtime",
  });

  return {
    valid: result.valid,
    error_count: result.errors.length,
    warning_count: result.warnings.length,
    errors: result.errors.map(serializeIssue),
    warnings: result.warnings.map(serializeIssue),
    statistics: result.statistics || {},
    suggestions: (result.suggestions || []).slice(0, 5),
  };
}

async function main() {
  const validator = await createValidator();
  process.stdout.write(JSON.stringify({ ready: true }) + "\n");

  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  for await (const line of rl) {
    if (!line.trim()) {
      continue;
    }

    let request;
    try {
      request = JSON.parse(line);
    } catch (error) {
      process.stdout.write(
        JSON.stringify({ error: `Invalid request payload: ${error.message}` }) + "\n"
      );
      continue;
    }

    try {
      const result = await validateWorkflow(validator, request.workflow || {});
      process.stdout.write(JSON.stringify({ result }) + "\n");
    } catch (error) {
      process.stdout.write(
        JSON.stringify({
          error: error instanceof Error ? error.message : String(error),
        }) + "\n"
      );
    }
  }
}

main().catch((error) => {
  process.stderr.write(
    `${error instanceof Error ? error.stack || error.message : String(error)}\n`
  );
  process.stdout.write(
    JSON.stringify({
      ready: false,
      error: error instanceof Error ? error.message : String(error),
    }) + "\n"
  );
  process.exit(1);
});
