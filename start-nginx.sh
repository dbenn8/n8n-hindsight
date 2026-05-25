#!/bin/sh
envsubst '${HINDSIGHT_API_TENANT_API_KEY}' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf
exec nginx -g "daemon off;"
