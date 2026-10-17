#!/usr/bin/env bash
# Runs inside LocalStack when it is ready: creates both queues with dead-letter queues, and the bucket.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
MAX_RECEIVE_COUNT="${SQS_MAX_RECEIVE_COUNT:-5}"
VISIBILITY_TIMEOUT="${SQS_VISIBILITY_TIMEOUT_SECONDS:-300}"
BUCKET="${S3_EXPORTS_BUCKET:-apa-report-exports}"
ATTRS="$(mktemp)"

for name in apa-planning apa-generation; do
  dlq_url=$(awslocal sqs create-queue --region "$REGION" --queue-name "${name}-dlq" \
    --attributes MessageRetentionPeriod=1209600 --query QueueUrl --output text)
  dlq_arn=$(awslocal sqs get-queue-attributes --region "$REGION" --queue-url "$dlq_url" \
    --attribute-names QueueArn --query Attributes.QueueArn --output text)
  cat > "$ATTRS" <<JSON
{
  "VisibilityTimeout": "${VISIBILITY_TIMEOUT}",
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"${dlq_arn}\",\"maxReceiveCount\":\"${MAX_RECEIVE_COUNT}\"}"
}
JSON
  awslocal sqs create-queue --region "$REGION" --queue-name "$name" --attributes "file://${ATTRS}" >/dev/null
  echo "created queue ${name} (dead-letter queue ${name}-dlq, maxReceiveCount ${MAX_RECEIVE_COUNT})"
done
rm -f "$ATTRS"

awslocal s3 mb "s3://${BUCKET}" --region "$REGION" >/dev/null 2>&1 || true
echo "created bucket ${BUCKET}"
