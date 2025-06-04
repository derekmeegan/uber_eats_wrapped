import { Stagehand, Page, BrowserContext, ObserveResult } from "@browserbasehq/stagehand";
import StagehandConfig from "./stagehand.config.js";
import chalk from "chalk";
import boxen from "boxen";
import { simpleCache, readCache } from "./utils.js";
import { z } from "zod";
import fs from "fs";

async function main({
  page,
  context,
  stagehand,
}: {
  page: Page; // Playwright Page with act, extract, and observe methods
  context: BrowserContext; // Playwright BrowserContext
  stagehand: Stagehand; // Stagehand instance
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
      }
    }
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

  // write orders json to file 
  fs.writeFileSync("orders.json", JSON.stringify(data.orders, null, 2));

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

/**
 * This is the main function that runs when you do npm run start
 *
 * YOU PROBABLY DON'T NEED TO MODIFY ANYTHING BELOW THIS POINT!
 *
 */
async function run() {
  const stagehand = new Stagehand({
    ...StagehandConfig,
  });
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
    stagehand
  });
  await stagehand.close();
  console.log(
    `\n🤘 Thanks so much for using Stagehand! Reach out to us on Slack if you have any feedback: ${chalk.blue(
      "https://stagehand.dev/slack",
    )}\n`,
  );
}

run();
