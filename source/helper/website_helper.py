# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# ###############################################################################
# PURPOSE:
#   * Copy files for the website from a source build bucket to a
#   deployment bucket.
#   * Purge deployment bucket so the bucket can be removed when users delete
#   the stack.
#   * This function starts as a CloudFormation custom resource in
#   deployment/web.yaml.
#
###############################################################################

import json
import logging
import os
from urllib.request import HTTPHandler, Request, build_opener

import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

s3 = boto3.resource("s3")
s3_client = boto3.client("s3")
error_response_str = "Unexpected event received from CloudFormation"


def send_response(event, context, response_status, response_data):
    """
    Send a resource manipulation status response to CloudFormation
    """
    response_body = json.dumps(
        {
            "Status": response_status,
            "Reason": "See the details in CloudWatch Log Stream: "
            + context.log_stream_name,
            "PhysicalResourceId": context.log_stream_name,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": response_data,
        }
    )

    LOGGER.info("ResponseURL: {s}".format(s=event["ResponseURL"]))
    LOGGER.info("ResponseBody: {s}".format(s=response_body))

    opener = build_opener(HTTPHandler)
    request = Request(event["ResponseURL"], data=response_body.encode("utf-8"))
    request.add_header("Content-Type", "")
    request.add_header("Content-Length", str(len(response_body)))
    request.get_method = lambda: "PUT"
    response = opener.open(request)
    LOGGER.info("Status code: {s}".format(s=response.getcode))
    LOGGER.info("Status message: {s}".format(s=response.msg))


def write_to_s3(event, context, bucket, key, body):
    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=body)
    except Exception as e:
        LOGGER.info("Unable to write file to s3: {e}".format(e=e))
        send_response(
            event,
            context,
            "FAILED",
            {
                "Message": "Failed to write file to s3 after variable replacement"
            },
        )
    else:
        LOGGER.info("Wrote file back to s3 after variable replacement")


def read_from_s3(event, context, bucket, key):
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as e:
        LOGGER.info(
            "Unable to read key: {key} in from s3 bucket: {bucket}. Error: {e}".format(
                e=e, key=key, bucket=bucket
            )
        )
        send_response(
            event,
            context,
            "FAILED",
            {"Message": "Failed to read file from s3"},
        )
    else:
        results = obj["Body"].read().decode("utf-8")
        return results


def copy_source(event, context):
    try:
        source_bucket = event["ResourceProperties"]["WebsiteCodeBucket"]
        source_key = event["ResourceProperties"]["WebsiteCodePrefix"]
        website_bucket = event["ResourceProperties"]["DeploymentBucket"].split(
            "."
        )[0]
    except KeyError as e:
        LOGGER.info(
            "Failed to retrieve required values from the CloudFormation event: {e}".format(
                e=e
            )
        )
        send_response(
            event,
            context,
            "FAILED",
            {
                "Message": "Failed to retrieve required values from the CloudFormation event"
            },
        )
    else:
        try:
            LOGGER.info("Checking if custom environment variables are present")

            replace_env_variables = None

            try:
                user_pool_id = os.environ["UserPoolId"]
                region = os.environ["AwsRegion"]
                client_id = os.environ["PoolClientId"]
                identity_id = os.environ["IdentityPoolId"]
                data_bucket_name = os.environ["DataBucketName"]
                artifact_bucket_name = os.environ["ArtifactBucketName"]
                encryption_mode = os.environ["EncryptionMode"]
            except KeyError:
                replace_env_variables = False
            else:
                new_variables = {
                    "AWS_REGION": region,
                    "USER_POOL_ID": user_pool_id,
                    "USER_POOL_CLIENT_ID": client_id,
                    "IDENTITY_POOL_ID": identity_id,
                    "DATA_BUCKET_NAME": data_bucket_name,
                    "ARTIFACT_BUCKET_NAME": artifact_bucket_name,
                    "ENCRYPTION_MODE": encryption_mode,
                }
                replace_env_variables = True
                LOGGER.info("New variables: {v}".format(v=new_variables))
                with open("./webapp-manifest.json", encoding="utf-8") as file:
                    manifest = json.load(file)
                    print("UPLOADING FILES::")
                    for key in manifest:
                        print(
                            "s3://"
                            + source_bucket
                            + "/"
                            + source_key
                            + "/"
                            + key
                        )
                        copy_source_dict = {
                            "Bucket": source_bucket,
                            "Key": source_key + "/" + key,
                        }
                        s3.meta.client.copy(
                            copy_source_dict, website_bucket, key
                        )
                        if (
                            replace_env_variables is True
                            and key == "runtimeConfig.json"
                        ):
                            LOGGER.info("updating runtimeConfig.json")
                            write_to_s3(
                                event,
                                context,
                                website_bucket,
                                key,
                                json.dumps(new_variables),
                            )

        except Exception as ex:
            LOGGER.info(
                "Unable to copy website source code into the website bucket: {ex}".format(
                    ex=ex
                )
            )
            send_response(
                event, context, "FAILED", {"Message": error_response_str}
            )

        send_response(
            event,
            context,
            "SUCCESS",
            {"Message": "Resource creation successful!"},
        )


def purge_bucket(event, context):
    try:
        bucket_name = event["ResourceProperties"]["DeploymentBucket"].split(
            "."
        )[0]
        # Stop access logging to bucket.
        # If we don't do this then s3 may create access logs in the bucket
        # after the following request to delete all objects,
        # thus preventing CloudFormation from removing the bucket
        # when the stack is deleted.
        LOGGER.info("Stop access logging to bucket " + bucket_name)
        s3_client.put_bucket_logging(
            Bucket=bucket_name,
            BucketLoggingStatus={},
        )
        LOGGER.info("Purging bucket " + bucket_name)
        bucket = s3.Bucket(bucket_name)
        bucket.objects.all().delete()
    except Exception as ex:
        LOGGER.info(
            "Unable to purge artifact bucket while deleting stack: {ex}".format(
                ex=ex
            )
        )
        send_response(
            event, context, "FAILED", {"Message": error_response_str}
        )
    else:
        send_response(
            event,
            context,
            "SUCCESS",
            {"Message": "Resource creation successful!"},
        )


def lambda_handler(event, context):
    """
    Handle Lambda event from AWS
    """
    try:
        LOGGER.info("REQUEST RECEIVED:\n {s}".format(s=event))
        LOGGER.info("REQUEST RECEIVED:\n {s}".format(s=context))
        if event["RequestType"] == "Create":
            LOGGER.info("CREATE!")
            copy_source(event, context)
        elif event["RequestType"] == "Update":
            LOGGER.info("UPDATE!")
            copy_source(event, context)
        elif event["RequestType"] == "Delete":
            LOGGER.info("DELETE!")
            purge_bucket(event, context)
            send_response(
                event,
                context,
                "SUCCESS",
                {"Message": "Resource deletion successful!"},
            )
        else:
            LOGGER.info("FAILED!")
            send_response(
                event, context, "FAILED", {"Message": error_response_str}
            )
    except Exception as ex:
        LOGGER.info("FAILED!")
        send_response(
            event,
            context,
            "FAILED",
            {"Message": "Exception during processing: {ex}".format(ex=ex)},
        )
