import type { ConstructorParams } from "@browserbasehq/stagehand";
import dotenv from "dotenv";

dotenv.config();

const StagehandConfig: ConstructorParams = {
  env: "BROWSERBASE",
  verbose: 1,
  domSettleTimeoutMs: 30_000,
  
  // LLM configuration
  modelName: "gpt-4.1-mini",
  modelClientOptions: {
    apiKey: process.env.OPENAI_API_KEY, /* Model API key */
  },

  // Browserbase configuration
  apiKey: process.env.BROWSERBASE_API_KEY,
  projectId: process.env.BROWSERBASE_PROJECT_ID,
  browserbaseSessionCreateParams: {
    projectId: process.env.BROWSERBASE_PROJECT_ID!,
    proxies: [{
      "type": "browserbase",
      "geolocation": {
        "city": "PHILADELPHIA",
        "state": "PA",
        "country": "US"
      }
    }],
    browserSettings: {
      solveCaptchas: false,
      context: {
        "id": "24ab1478-599c-4910-b7e9-de32e2925756",
        "persist": true
      },
      viewport: {
        width: 1024,
        height: 768,
      },
    },
    region: "us-east-1"
  },
  localBrowserLaunchOptions: {
    viewport: {
      width: 1024,
      height: 768,
    },
  },
};

export default StagehandConfig;
