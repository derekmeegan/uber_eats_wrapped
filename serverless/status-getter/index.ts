import { APIGatewayProxyEvent, APIGatewayProxyResult } from 'aws-lambda';
import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';

const dynamoClient = new DynamoDBClient({ region: process.env.AWS_REGION });

export const handler = async (event: APIGatewayProxyEvent): Promise<APIGatewayProxyResult> => {
  const headers = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-Api-Key',
  };

  try {
    const userEmail = event.pathParameters?.userEmail;

    if (!userEmail) {
      return {
        statusCode: 400,
        headers,
        body: JSON.stringify({ error: 'userEmail parameter is required' }),
      };
    }

    const decodedEmail = decodeURIComponent(userEmail);

    const getItemCommand = new GetItemCommand({
      TableName: process.env.DYNAMODB_TABLE_NAME,
      Key: {
        userEmail: { S: decodedEmail },
      },
    });

    const result = await dynamoClient.send(getItemCommand);

    if (!result.Item) {
      return {
        statusCode: 404,
        headers,
        body: JSON.stringify({ 
          error: 'Extraction status not found',
          userEmail: decodedEmail 
        }),
      };
    }

    const status = {
      userEmail: result.Item.userEmail?.S,
      status: result.Item.status?.S,
      browserbaseSessionId: result.Item.browserbaseSessionId?.S,
      message: result.Item.message?.S,
      timestamp: result.Item.timestamp?.S,
    };

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify(status),
    };
  } catch (error) {
    console.error('Error getting extraction status:', error);
    return {
      statusCode: 500,
      headers,
      body: JSON.stringify({ error: 'Internal server error' }),
    };
  }
}; 