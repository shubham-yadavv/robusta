import hashlib
import hmac
import logging
import traceback

import websocket
import json
import os
import time
from threading import Thread

from ..core.model.events import ExecutionBaseEvent
from ..model.playbook_action import PlaybookAction
from ..core.playbooks.playbooks_event_handler import PlaybooksEventHandler
from ..core.model.env_vars import TARGET_ID, INCOMING_REQUEST_TIME_WINDOW_SECONDS
from ..core.reporting.callbacks import *

WEBSOCKET_RELAY_ADDRESS = os.environ.get(
    "WEBSOCKET_RELAY_ADDRESS", "wss://relay.robusta.dev"
)
INCOMING_RECEIVER_ENABLED = os.environ.get("INCOMING_RECEIVER_ENABLED", "True")
RECEIVER_ENABLE_WEBSOCKET_TRACING = "True" == os.environ.get(
    "RECEIVER_ENABLE_WEBSOCKET_TRACING"
)
INCOMING_WEBSOCKET_RECONNECT_DELAY_SEC = int(
    os.environ.get("INCOMING_WEBSOCKET_RECONNECT_DELAY_SEC", 3)
)


class ActionRequestBody(BaseModel):
    account_id: str
    cluster_name: str
    action_name: str
    timestamp: int
    action_params: dict = None
    sinks: Optional[List[str]] = None
    origin: str = None


class ActionRequest(BaseModel):
    signature: str
    body: ActionRequestBody


class ActionRequestReceiver:
    def __init__(self, event_handler: PlaybooksEventHandler):
        self.event_handler = event_handler
        self.active = True
        self.start_incoming_receiver()

    def __run_external_action_request(self, callback_request: ExternalActionRequest):
        execution_event = ExecutionBaseEvent(
            named_sinks=callback_request.sinks,
        )
        logging.info(
            f"got callback `{callback_request.action_name}` {callback_request.action_params}"
        )
        action = PlaybookAction(
            action_name=callback_request.action_name,
            action_params=callback_request.action_params,
        )
        self.event_handler.run_actions(execution_event, [action])

    def start_incoming_receiver(self):
        if INCOMING_RECEIVER_ENABLED != "True":
            logging.info(
                "outgoing messages only mode. Incoming event receiver not initialized"
            )
            return

        if WEBSOCKET_RELAY_ADDRESS == "":
            logging.warning("relay address empty. Not initializing relay")
            return

        websocket.enableTrace(RECEIVER_ENABLE_WEBSOCKET_TRACING)
        receiver_thread = Thread(target=self.run_forever)
        receiver_thread.start()

    def run_forever(self):
        logging.info("starting relay receiver")
        while self.active:
            ws = websocket.WebSocketApp(
                WEBSOCKET_RELAY_ADDRESS,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
            )
            ws.run_forever()
            logging.info("relay websocket closed")
            time.sleep(INCOMING_WEBSOCKET_RECONNECT_DELAY_SEC)

    def stop(self):
        logging.info(f"Stopping incoming receiver")
        self.active = False

    def on_message(self, ws, message):
        # TODO: use typed pydantic classes here?
        logging.debug(f"received incoming message {message}")
        incoming_event = json.loads(message)
        actions = incoming_event.get("actions", None)
        if actions:  # this is slack callback format
            for action in actions:
                try:
                    incoming_request = IncomingRequest.parse_raw(action["value"])
                    self.__run_external_action_request(
                        incoming_request.incoming_request
                    )
                except Exception:
                    logging.error(
                        f"Failed to run incoming event {incoming_event}",
                        traceback.print_exc(),
                    )
        else:  # assume it's ActionRequest format
            try:
                action_request = ActionRequest(**incoming_event)
                if not self.__validate_request(action_request):
                    logging.error(f"Failed to validate action request {action_request}")
                    return

                incoming_request = ExternalActionRequest(
                    target_id="",
                    action_name=action_request.body.action_name,
                    action_params=action_request.body.action_params,
                    sinks=action_request.body.sinks,
                    origin=action_request.body.origin,
                )
                self.__run_external_action_request(incoming_request)
            except Exception:
                logging.error(
                    f"Failed to run incoming event {incoming_event}",
                    traceback.print_exc(),
                )

    def on_error(self, ws, error):
        logging.info(f"Relay websocket error: {error}")

    def on_open(self, ws):
        logging.info(f"connecting to server as {TARGET_ID}")
        account_id = self.event_handler.get_global_config().get("account_id")
        cluster_name = self.event_handler.get_global_config().get("cluster_name")
        open_payload = {"action": "auth", "key": "dummy key", "target_id": TARGET_ID}
        if account_id and cluster_name:
            open_payload["account_id"] = account_id
            open_payload["cluster_name"] = cluster_name
        ws.send(json.dumps(open_payload))

    def __validate_request(self, action_request: ActionRequest) -> bool:
        format_req = str.encode(f"v0:{action_request.body.json(exclude_none=True)}")
        signing_key = self.event_handler.get_global_config().get("signing_key")
        if not signing_key:
            logging.error(
                f"Signing key not available. Cannot verify request {action_request}"
            )
            return False

        if (
            time.time() - action_request.body.timestamp
            > INCOMING_REQUEST_TIME_WINDOW_SECONDS
        ):
            logging.error(
                f"Rejecting incoming request because it's too old. Cannot verify request {action_request}"
            )
            return False

        encoded_secret = str.encode(signing_key)
        request_hash = hmac.new(encoded_secret, format_req, hashlib.sha256).hexdigest()
        generated_signature = f"v0={request_hash}"
        return hmac.compare_digest(generated_signature, action_request.signature)