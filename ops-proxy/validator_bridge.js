#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

function resolveInstallRoot() {
  if (process.env.N8N_MCP_INSTALL_ROOT) {
    return process.env.N8N_MCP_INSTALL_ROOT;
  }

  const entryPath = require.resolve("n8n-mcp");
  let current = path.dirname(entryPath);

  for (let index = 0; index < 5; index += 1) {
    if (
      fs.existsSync(path.join(current, "dist")) &&
      fs.existsSync(path.join(current, "data", "nodes.db"))
    ) {
      return current;
    }

    const parent = path.dirname(current);
    if (parent === current) {
      break;
    }
    current = parent;
  }

  throw new Error(`Unable to locate n8n-mcp install root from ${entryPath}`);
}

const installRoot = resolveInstallRoot();
const distRoot = path.join(installRoot, "dist");
const dbPath = path.join(installRoot, "data", "nodes.db");

const { SQLiteStorageService } = require(path.join(
  distRoot,
  "services/sqlite-storage-service"
));
const { NodeRepository } = require(path.join(distRoot, "database/node-repository"));
const { WorkflowValidator } = require(path.join(
  distRoot,
  "services/workflow-validator"
));
const { EnhancedConfigValidator } = require(path.join(
  distRoot,
  "services/enhanced-config-validator"
));
const { ConfigValidator } = require(path.join(
  distRoot,
  "services/config-validator"
));

// Keep behavior identical to the plugin's local bridge: skip options
// validation for properties with empty options arrays (dynamically loaded
// via loadOptionsMethod). Without this, cloud and local validators disagree
// on exactly those workflows, defeating the preflight parity guarantee.
const origValidatePropertyTypes = ConfigValidator.validatePropertyTypes;
ConfigValidator.validatePropertyTypes = function (properties, config, errors) {
  const filteredProperties = properties.filter(function (prop) {
    if (prop && prop.type === "options" && prop.options && Array.isArray(prop.options) && prop.options.length === 0) {
      return false;
    }
    return true;
  });
  origValidatePropertyTypes.call(this, filteredProperties, config, errors);
};

function serializeIssue(issue) {
  return {
    type: issue.type,
    message: issue.message,
    node: issue.nodeName || null,
  };
}

async function validate(workflow) {
  const storage = new SQLiteStorageService(dbPath);
  const repo = new NodeRepository(storage);
  EnhancedConfigValidator.initializeSimilarityServices(repo);
  const validator = new WorkflowValidator(repo, EnhancedConfigValidator);

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
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }

  try {
    const workflow = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const result = await validate(workflow);
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stdout.write(
      JSON.stringify({
        valid: false,
        error_count: 1,
        warning_count: 0,
        errors: [
          {
            type: "validator_bridge_error",
            message: error instanceof Error ? error.message : String(error),
            node: null,
          },
        ],
        warnings: [],
        statistics: {},
        suggestions: [],
      })
    );
  }
}

main().catch((error) => {
  process.stderr.write(
    `${error instanceof Error ? error.stack || error.message : String(error)}\n`
  );
  process.exit(1);
});
