# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.

import datetime
from copy import deepcopy
from typing import Any, Iterator, Optional

from botocore.client import BaseClient as BotoBaseClient

from share import ExpandEventListFromField, ProtocolMultiline, shared_logger
from storage import ProtocolStorage, StorageFactory

from .event import _default_event
from .utils import get_account_id_from_arn, get_kinesis_stream_name_type_and_region_from_arn


def _handle_kinesis_continuation(
    sqs_client: BotoBaseClient,
    sqs_continuing_queue: str,
    last_ending_offset: Optional[int],
    last_event_expanded_offset: Optional[int],
    kinesis_record: dict[str, Any],
    event_input_id: str,
    config_yaml: str,
) -> None:
    """
    Handler of the continuation queue for kinesis data stream inputs
    If a kinesis data stream records batch cannot be fully processed before the
    timeout of the lambda this handler will be called: it will
    send new sqs messages for the unprocessed records in the batch to the
    internal continuing sqs queue
    """

    sequence_number = kinesis_record["kinesis"]["sequenceNumber"]
    stream_type, stream_name, _ = get_kinesis_stream_name_type_and_region_from_arn(event_input_id)

    message_attributes = {
        "config": {"StringValue": config_yaml, "DataType": "String"},
        "originalStreamType": {"StringValue": stream_type, "DataType": "String"},
        "originalStreamName": {"StringValue": stream_name, "DataType": "String"},
        "originalSequenceNumber": {"StringValue": sequence_number, "DataType": "String"},
        "originalEventSourceARN": {"StringValue": event_input_id, "DataType": "String"},
    }

    if last_ending_offset is not None:
        message_attributes["originalLastEndingOffset"] = {"StringValue": str(last_ending_offset), "DataType": "Number"}

    if last_event_expanded_offset is not None:
        message_attributes["originalLastEventExpandedOffset"] = {
            "StringValue": str(last_event_expanded_offset),
            "DataType": "Number",
        }

    kinesis_data: bytes = kinesis_record["kinesis"]["data"]
    message_body: str = kinesis_data.decode("utf-8")

    sqs_client.send_message(
        QueueUrl=sqs_continuing_queue,
        MessageBody=message_body,
        MessageAttributes=message_attributes,
    )

    shared_logger.debug(
        "continuing",
        extra={
            "sqs_continuing_queue": sqs_continuing_queue,
            "last_ending_offset": last_ending_offset,
            "last_event_expanded_offset": last_event_expanded_offset,
            "sequence_number": sequence_number,
        },
    )


def _handle_kinesis_record(
    event: dict[str, Any],
    input_id: str,
    expand_event_list_from_field: ExpandEventListFromField,
    json_content_type: Optional[str],
    multiline_processor: Optional[ProtocolMultiline],
) -> Iterator[tuple[dict[str, Any], int, Optional[int], int]]:
    """
    Handler for kinesis data stream inputs.
    It iterates through kinesis records in the kinesis trigger and process
    the content of kinesis.data payload
    """
    account_id = get_account_id_from_arn(input_id)

    for kinesis_record_n, kinesis_record in enumerate(event["Records"]):
        storage: ProtocolStorage = StorageFactory.create(
            storage_type="payload",
            payload=kinesis_record["kinesis"]["data"],
            json_content_type=json_content_type,
            expand_event_list_from_field=expand_event_list_from_field,
            multiline_processor=multiline_processor,
        )

        stream_type, stream_name, aws_region = get_kinesis_stream_name_type_and_region_from_arn(
            kinesis_record["eventSourceARN"]
        )

        shared_logger.info("kinesis event")

        events = storage.get_by_lines(range_start=0)

        for log_event, starting_offset, ending_offset, event_expanded_offset in events:
            assert isinstance(log_event, bytes)

            es_event = deepcopy(_default_event)
            es_event["@timestamp"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            es_event["fields"]["message"] = log_event.decode("UTF-8")

            es_event["fields"]["log"]["offset"] = starting_offset

            es_event["fields"]["log"]["file"]["path"] = kinesis_record["eventSourceARN"]

            es_event["fields"]["aws"] = {
                "kinesis": {
                    "type": stream_type,
                    "name": stream_name,
                    "sequence_number": kinesis_record["kinesis"]["sequenceNumber"],
                }
            }

            es_event["fields"]["cloud"]["region"] = aws_region
            es_event["fields"]["cloud"]["account"] = {"id": account_id}

            yield es_event, ending_offset, event_expanded_offset, kinesis_record_n
