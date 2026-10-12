/// <reference path="./.sst/platform/config.d.ts" />

/**
 * Deploys generate_team_insight as an HTTP endpoint:
 *
 *   POST /teams/{team_id}/insights   body: {"events": [...]}
 *
 * The handler lives in the `functions` uv workspace member and depends on the
 * `app` package at the repo root. SST packages it with uv.
 */
export default $config({
  app(input) {
    return {
      name: "team-insight",
      removal: input?.stage === "production" ? "retain" : "remove",
      protect: input?.stage === "production",
      home: "aws",
    };
  },
  async run() {
    // Uncomment once a real LLM provider is wired in functions/src/functions/api.py,
    // then set it with: npx sst secret set LlmApiKey <value> --stage <stage>
    // const llmApiKey = new sst.Secret("LlmApiKey");

    const api = new sst.aws.ApiGatewayV2("Api");

    api.route("POST /teams/{team_id}/insights", {
      handler: "functions/src/functions/api.handler",
      runtime: "python3.12",
      // Two LLM attempts of LLM_TIMEOUT_SECONDS each plus overhead, below
      // API Gateway's 29s integration limit.
      timeout: "25 seconds",
      memory: "256 MB",
      environment: {
        LLM_TIMEOUT_SECONDS: "10",
        // LLM_API_KEY: llmApiKey.value,
      },
    });

    return { api: api.url };
  },
});
