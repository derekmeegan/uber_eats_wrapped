from aws_cdk import (
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_logs as logs, 
    aws_dynamodb as dynamodb,
    aws_s3 as s3,
    aws_s3_notifications as s3_notifications,
    aws_secretsmanager as secretsmanager,
    aws_apigateway as apigateway,
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct

class UberEatsAnalyzerStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create S3 bucket for orders
        orders_bucket = s3.Bucket(self, "OrdersBucket",
            bucket_name="ubereats-orders-bucket",
            removal_policy=RemovalPolicy.DESTROY,
        )

        browser_base_project_id = secretsmanager.Secret.from_secret_name_v2(
            self,
            "BrowserbaseProjectId",
            "BrowserbaseLambda/BrowserbaseProjectId"
        )
        
        browserbase_api_key_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "BrowserbaseApiKey",
            "BrowserbaseLambda/BrowserbaseApiKey"
        )

        derek_sendgrid_api_key = secretsmanager.Secret.from_secret_name_v2(
            self,
            "DerekSendgridApiKey",
            "DerekSendgridApiKey"
        )

        derek_sender_email = secretsmanager.Secret.from_secret_name_v2(
            self,
            "DerekSenderEmail",
            "DerekSenderEmail"
        )

        openai_api_key = secretsmanager.Secret.from_secret_name_v2(
            self,
            "OpenAIApiKey",
            "OpenAIApiKey"
        )

        # Create DynamoDB table for tracking extraction status  
        extraction_status_table = dynamodb.Table(
            self,
            "ExtractionStatusTable",
            table_name="uber-eats-extraction-status",
            partition_key=dynamodb.Attribute(
                name="userEmail",
                type=dynamodb.AttributeType.STRING
            ),
            removal_policy=RemovalPolicy.DESTROY
        )

        analyzer_function = PythonFunction(
            self,
            "AnalyzerFunction",
            runtime=lambda_.Runtime.PYTHON_3_9,
            entry="../serverless/analzyer",
            index="analyzer.py",
            handler="lambda_handler",
            timeout=Duration.minutes(10),
            environment={
                "DEREK_SENDGRID_API_KEY": derek_sendgrid_api_key.secret_value.to_string(),
                "DEREK_SENDER_EMAIL": derek_sender_email.secret_value.to_string(),
                "S3_BUCKET_NAME": orders_bucket.bucket_name,
            },
        )

        orders_bucket.grant_read_write(analyzer_function)

        # Create execution roles
        extractor_execution_role = iam.Role(
            self, "ExtractorLambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ]
        )
        browserbase_api_key_secret.grant_read(extractor_execution_role)
        browser_base_project_id.grant_read(extractor_execution_role)
        openai_api_key.grant_read(extractor_execution_role)
        orders_bucket.grant_read_write(extractor_execution_role)
        extraction_status_table.grant_read_write_data(extractor_execution_role)

        status_getter_execution_role = iam.Role(
            self, "StatusGetterLambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ]
        )
        extraction_status_table.grant_read_data(status_getter_execution_role)

        # Create TypeScript Lambda function for extraction
        extractor_function = lambda_.Function(
            self,
            "ExtractorFunction",
            runtime=lambda_.Runtime.NODEJS_20_X,
            code=lambda_.Code.from_asset("../serverless/extractor"),
            handler="index.handler",
            timeout=Duration.minutes(15),
            memory_size=1024,
            role=extractor_execution_role,
            environment={
                "BROWSERBASE_API_KEY_SECRET_ARN": browserbase_api_key_secret.secret_arn,
                "BROWSERBASE_PROJECT_ID_SECRET_ARN": browser_base_project_id.secret_arn,
                "OPENAI_API_KEY_SECRET_ARN": openai_api_key.secret_arn,
                "S3_BUCKET_NAME": orders_bucket.bucket_name,
                "DYNAMODB_TABLE_NAME": extraction_status_table.table_name,
            },
        )

        # Create status getter Lambda
        status_getter_function = lambda_.Function(
            self,
            "StatusGetterFunction",
            runtime=lambda_.Runtime.NODEJS_20_X,
            code=lambda_.Code.from_asset("../serverless/status-getter"),
            handler="index.handler",
            timeout=Duration.seconds(30),
            role=status_getter_execution_role,
            environment={
                "DYNAMODB_TABLE_NAME": extraction_status_table.table_name,
            },
        )

        # Add S3 event notification to trigger analyzer when orders are uploaded
        orders_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3_notifications.LambdaDestination(analyzer_function),
            s3.NotificationKeyFilter(prefix="orders/", suffix=".json")
        )

        # Create async Lambda integration for extraction
        async_lambda_integration = apigateway.LambdaIntegration(
            extractor_function,
            proxy=False,
            request_parameters={
                'integration.request.header.X-Amz-Invocation-Type': "'Event'"
            },
            integration_responses=[
                apigateway.IntegrationResponse(
                    status_code="202",
                )
            ],
        )

        # Create sync Lambda integration for status checking
        status_getter_integration = apigateway.LambdaIntegration(
            status_getter_function,
            proxy=True
        )

        # Create API Gateway logs
        api_log_group = logs.LogGroup(
            self, "UberEatsApiAccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY
        )

        # Create API Gateway
        api = apigateway.RestApi(
            self,
            "UberEatsAsyncApi",
            rest_api_name="Uber Eats Async API",
            description="API for Uber Eats extraction service",
            deploy_options=apigateway.StageOptions(
                stage_name="v1",
                access_log_destination=apigateway.LogGroupLogDestination(api_log_group),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=True
            ),
            cloud_watch_role=True,
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "X-Amz-Date", "Authorization", "X-Api-Key", "X-Amz-Security-Token"]
            )
        )

        # Request models and validation
        extract_request_model = api.add_model("ExtractRequestModel",
            content_type="application/json",
            model_name="ExtractRequestModel",
            schema=apigateway.JsonSchema(
                schema=apigateway.JsonSchemaVersion.DRAFT4,
                title="ExtractRequest",
                type=apigateway.JsonSchemaType.OBJECT,
                required=["userEmail"],
                properties={
                    "userEmail": apigateway.JsonSchema(
                        type=apigateway.JsonSchemaType.STRING,
                        format="email",
                        description="User email address to track the extraction job"
                    )
                }
            )
        )

        body_validator = api.add_request_validator("BodyValidator",
            request_validator_name="ValidateRequestBody",
            validate_request_body=True,
            validate_request_parameters=False
        )

        params_validator = api.add_request_validator("ParameterValidator",
            request_validator_name="ValidateRequestParameters",
            validate_request_body=False,
            validate_request_parameters=True
        )

        # API routes
        extract_resource = api.root.add_resource("extract")
        extract_resource.add_method(
            "POST",
            async_lambda_integration,
            api_key_required=True,
            request_validator=body_validator,
            request_models={
                "application/json": extract_request_model
            },
            method_responses=[
                apigateway.MethodResponse(status_code="202"),
                apigateway.MethodResponse(status_code="400")
            ]
        )

        status_resource = extract_resource.add_resource("{userEmail}")
        status_resource.add_method(
            "GET",
            status_getter_integration,
            api_key_required=True,
            request_validator=params_validator,
            request_parameters={
                'method.request.path.userEmail': True
            },
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
                apigateway.MethodResponse(status_code="404"),
                apigateway.MethodResponse(status_code="400"),
                apigateway.MethodResponse(status_code="500")
            ]
        )

        # API Key and Usage Plan
        api_key = api.add_api_key("UberEatsApiKey",
            api_key_name="uber-eats-api-key"
        )

        usage_plan = api.add_usage_plan("UberEatsUsagePlan",
            name="UberEatsBasic",
            throttle=apigateway.ThrottleSettings(
                rate_limit=10,
                burst_limit=5
            ),
            api_stages=[apigateway.UsagePlanPerApiStage(
                api=api,
                stage=api.deployment_stage
            )]
        )
        usage_plan.add_api_key(api_key)

        # Outputs
        CfnOutput(
            self,
            "ApiExtractEndpointUrl",
            value=f"{api.url}extract",
            description="API Gateway Endpoint URL for POST /extract"
        )
        
        CfnOutput(
            self,
            "ApiStatusEndpointBaseUrl",
            value=f"{api.url}extract/",
            description="Base URL for GETting extraction status (append {userEmail})"
        )
        
        CfnOutput(
            self,
            "ApiKeyId",
            value=api_key.key_id,
            description="API Key ID (use 'aws apigateway get-api-key --api-key <key-id> --include-value' to retrieve the key value)"
        )
        
        CfnOutput(
            self,
            "OrdersBucketName", 
            value=orders_bucket.bucket_name,
            description="S3 bucket for storing extracted orders"
        )

        CfnOutput(
            self,
            "DynamoTableName",
            value=extraction_status_table.table_name,
            description="DynamoDB table for extraction status"
        )
        
        CfnOutput(
            self,
            "ApiGatewayAccessLogGroupName", 
            value=api_log_group.log_group_name,
            description="Log Group Name for API Gateway Access Logs"
        )