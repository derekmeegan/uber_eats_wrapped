import { Stagehand, Page, BrowserContext, ObserveResult } from "@browserbasehq/stagehand";
import StagehandConfig from "./stagehand.config.js";
import chalk from "chalk";
import boxen from "boxen";
import { simpleCache, readCache } from "./utils.js";
import { z } from "zod";
import { SNSEvent } from 'aws-lambda';
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { DynamoDBClient, PutItemCommand, UpdateItemCommand } from "@aws-sdk/client-dynamodb";
import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";

// Initialize AWS clients
const s3Client = new S3Client({ region: process.env.AWS_REGION || 'us-east-1' });
const dynamoClient = new DynamoDBClient({ region: process.env.AWS_REGION || 'us-east-1' });
const secretsClient = new SecretsManagerClient({ region: process.env.AWS_REGION || 'us-east-1' });

// Helper function to get secret value
async function getSecretValue(secretArn: string): Promise<string> {
  const command = new GetSecretValueCommand({ SecretId: secretArn });
  const response = await secretsClient.send(command);
  return response.SecretString || '';
}

// Helper function to update DynamoDB status
async function updateStatus(userEmail: string, status: string, browserbaseSessionId?: string, message?: string, liveViewUrl?: string) {
  const timestamp = new Date().toISOString();
  
  const params: any = {
    TableName: process.env.DYNAMODB_TABLE_NAME,
    Key: {
      userEmail: { S: userEmail }
    },
    UpdateExpression: 'SET #status = :status, #timestamp = :timestamp',
    ExpressionAttributeNames: {
      '#status': 'status',
      '#timestamp': 'timestamp'
    },
    ExpressionAttributeValues: {
      ':status': { S: status },
      ':timestamp': { S: timestamp }
    }
  };

  if (browserbaseSessionId) {
    params.UpdateExpression += ', browserbaseSessionId = :sessionId';
    params.ExpressionAttributeValues[':sessionId'] = { S: browserbaseSessionId };
  }

  if (message) {
    params.UpdateExpression += ', message = :message';
    params.ExpressionAttributeValues[':message'] = { S: message };
  }

  if (liveViewUrl) {
    params.UpdateExpression += ', liveViewUrl = :liveViewUrl';
    params.ExpressionAttributeValues[':liveViewUrl'] = { S: liveViewUrl };
  }

  const updateCommand = new UpdateItemCommand(params);
  await dynamoClient.send(updateCommand);
}

async function main({
  page,
  context,
  stagehand,
  userEmail,
}: {
  page: Page; // Playwright Page with act, extract, and observe methods
  context: BrowserContext; // Playwright BrowserContext
  stagehand: Stagehand; // Stagehand instance
  userEmail: string; // User email for S3 key
}) {
  // Navigate to a URL
  await page.goto("https://www.ubereats.com/");

  await page.act('Close any modal if it is open')

  await page.act('Close any cookie popup')

  // determine if login button is visible
  const [loginButton] = await page.observe("Click the login button");

  // check if login button is a non empty array
  if ([loginButton].length > 0) {
    // prompt user to login in live viewer
    console.log("Login button is visible. User is not logged in, please login in the browser");

    // Get live view link for user login
    let liveViewUrl = '';
    try {
      if (stagehand.browserbaseSessionID) {
        // Use the standard Browserbase live view URL format
        liveViewUrl = `https://browserbase.com/sessions/${stagehand.browserbaseSessionID}?navbar=false`;
        console.log("Live view URL generated:", liveViewUrl);
      }
    } catch (error) {
      console.error("Error getting live view link:", error);
    }

    // Update status to indicate user needs to login
    await updateStatus(
      userEmail, 
      'awaiting_login', 
      stagehand.browserbaseSessionID, 
      'Please login using the browser session. The extraction will continue automatically once you are logged in.',
      liveViewUrl
    );

    let loggedIn = false;

    while (!loggedIn) {
      await page.waitForTimeout(30_000);
      // wait for user to login by observing if cart button is visible
      const [cartButton] = await page.observe("Click the cart button");

      const cartButtonFiltered = [cartButton].filter((button: ObserveResult) => button && button.description.toLowerCase().includes("cart"));
      console.log(cartButtonFiltered);
      if (cartButtonFiltered.length > 0) {

        const [modalAction] = await page.observe('if a modal or popup is open, close it by clicking outside of it');
        if ([modalAction].length > 0) {
          await page.mouse.click(0, 0);
        }
        
        loggedIn = true;
        console.log("User is logged in");
        
        // Update status to indicate user is logged in and extraction is continuing
        await updateStatus(
          userEmail, 
          'extracting', 
          stagehand.browserbaseSessionID, 
          'Thank you for logging in! We are now extracting your order data. You will receive an email with your analysis shortly.'
        );
      }
    }
  } else {
    // User is already logged in
    await updateStatus(
      userEmail, 
      'extracting', 
      stagehand.browserbaseSessionID, 
      'User is already logged in. Extracting order data...'
    );
  }

  await page.act('open the main navigation menu')

  await page.act('click on the orders tab')

  let canLoadMore = true;

  while (canLoadMore) {
    
    const instruction = "Click the show more button element at the bottom of the orders to load previous orders"
    let cachedAction = await readCache(instruction);

    if (!cachedAction) {

      const [loadMoreButton] = await page.observe(instruction);
      const loadMoreButtonFiltered = [loadMoreButton].filter((button: ObserveResult) => button.description.toLowerCase().includes("show more") && !button.selector.endsWith("text()[1]"));
      const actionToCache = loadMoreButtonFiltered[0];
      await simpleCache(instruction, actionToCache);
      cachedAction = actionToCache;
    }

    try {
      let result = await page.act(cachedAction);
      if (!result.success) {
        throw new Error(result.message);
      }
    } catch (error) {
      console.log(chalk.red("Failed to act with cached action:"), error);
      canLoadMore = false;
    }
    
    await page.waitForTimeout(10_000);
  }

  // extract the orders
  let data;
  let retries = 3;
  let delay = 1000; // Start with 1 second delay
  
  while (retries > 0) {
    try {
      data = await page.extract({
        instruction: "Extract the orders from the page",
        schema: z.object({
          orders: z.array(
            z.object({
              restaurantName: z.string(),
              date: z.string(),
              time: z.string(),
              total: z.number(),
              canceled: z.boolean(),
            })
          ),
        }),
      });
      break; // If successful, exit the loop
    } catch (error) {
      retries--;
      if (retries === 0) {
        console.error(chalk.red("Failed to extract orders after multiple attempts:"), error);
        throw error; // Re-throw if all retries failed
      }
      
      console.log(chalk.yellow(`Extraction failed. Retrying in ${delay/1000} seconds... (${retries} attempts left)`));
      await page.waitForTimeout(delay);
      delay *= 2; // Exponential backoff
    }
  }

  if (!data) {
    throw new Error("Failed to extract orders after multiple attempts");
  }

  // Upload orders to S3
  const bucketName = process.env.S3_BUCKET_NAME;
  if (!bucketName) {
    throw new Error("S3_BUCKET_NAME environment variable is required");
  }

  const s3Key = `orders/${userEmail}/orders.json`;
  const ordersJson = JSON.stringify(data.orders, null, 2);

  const uploadCommand = new PutObjectCommand({
    Bucket: bucketName,
    Key: s3Key,
    Body: ordersJson,
    ContentType: 'application/json',
  });

  try {
    await s3Client.send(uploadCommand);
    console.log(`Successfully uploaded orders to S3: s3://${bucketName}/${s3Key}`);
    
    // Update status to completed
    await updateStatus(userEmail, 'completed', stagehand.browserbaseSessionID, 'Extraction completed successfully! You will receive your analysis via email shortly.');
    
  } catch (error) {
    console.error("Failed to upload to S3:", error);
    await updateStatus(userEmail, 'error', stagehand.browserbaseSessionID, `Error uploading to S3: ${error instanceof Error ? error.message : 'Unknown error'}`);
    throw error;
  }

  stagehand.log({
    category: "create-browser-app",
    message: `Metrics`,
    auxiliary: {
      metrics: {
        value: JSON.stringify(stagehand.metrics),
        type: "object",
      },
    },
  });
}


exports.handler = async function(event: any) {
  // Extract user email from async API Gateway invocation
  const userEmail = event.userEmail;

  console.log(`Processing request for user: ${userEmail}`);

  try {
    // Initial status update
    await updateStatus(userEmail, 'starting', undefined, 'Initializing browser session...');

    // Get secrets from Secrets Manager
    const browserbaseApiKey = await getSecretValue(process.env.BROWSERBASE_API_KEY_SECRET_ARN!);
    const browserbaseProjectId = await getSecretValue(process.env.BROWSERBASE_PROJECT_ID_SECRET_ARN!);
    const openaiApiKey = await getSecretValue(process.env.OPENAI_API_KEY_SECRET_ARN!);

    // Update Stagehand config with retrieved secrets
    const configWithSecrets = {
      ...StagehandConfig,
      apiKey: browserbaseApiKey,
      projectId: browserbaseProjectId,
      browserbaseSessionCreateParams: {
        projectId: browserbaseProjectId,
        proxies: true,
      },
      modelClientOptions: {
        apiKey: openaiApiKey,
      },
    };

    const stagehand = new Stagehand(configWithSecrets);
    await stagehand.init();

  if (StagehandConfig.env === "BROWSERBASE" && stagehand.browserbaseSessionID) {
    console.log(
      boxen(
        `View this session live in your browser: \n${chalk.blue(
          `https://browserbase.com/sessions/${stagehand.browserbaseSessionID}`,
        )}`,
        {
          title: "Browserbase",
          padding: 1,
          margin: 3,
        },
      ),
    );
  }

    const page = stagehand.page;
    const context = stagehand.context;
    await main({
      page,
      context,
      stagehand,
      userEmail
    });
    await stagehand.close();
    console.log(
      `\n🤘 Thanks so much for using Stagehand! Reach out to us on Slack if you have any feedback: ${chalk.blue(
        "https://stagehand.dev/slack",
      )}\n`,
    );
  } catch (error) {
    console.error("Error in extraction process:", error);
    await updateStatus(userEmail, 'error', undefined, `Extraction failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
    throw error;
  }
}
