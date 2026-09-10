"""Minimal ONVIF PullPoint ingestion into durable camera events."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
import time
from xml.etree import ElementTree

from .events import parse_utc, utcnow


logger = logging.getLogger(__name__)


def _serialized(value):
    try:
        from zeep.helpers import serialize_object
        return serialize_object(value, target_cls=dict)
    except ImportError:
        return value


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _simple_items(value):
    result = {}
    for item in _walk(value):
        if isinstance(item, dict) and "Name" in item and "Value" in item:
            result[str(item["Name"])] = item["Value"]
    return result


def _message_element(notification):
    message = getattr(notification, "Message", None)
    if message is not None:
        element = getattr(message, "_value_1", None)
        if hasattr(element, "iter") and hasattr(element, "attrib"): return element
    if isinstance(notification, dict):
        message = notification.get("Message")
        while isinstance(message, dict) and "_value_1" in message:
            message = message["_value_1"]
        if hasattr(message, "iter") and hasattr(message, "attrib"): return message
    return None


def _xml_details(element):
    items = {}
    for child in element.iter():
        if str(child.tag).split("}")[-1] == "SimpleItem":
            name, value = child.attrib.get("Name"), child.attrib.get("Value")
            if name is not None: items[name] = value
    return {
        "utc_time": element.attrib.get("UtcTime"),
        "property_operation": element.attrib.get("PropertyOperation"),
        "items": items,
        "xml": ElementTree.tostring(element, encoding="unicode"),
    }


def _topic(value):
    for item in _walk(value):
        if not isinstance(item, dict): continue
        for key, child in item.items():
            if "topic" not in str(key).lower(): continue
            if isinstance(child, dict):
                return str(child.get("_value_1") or child.get("Value") or "")
            return str(child)
    return ""


def parse_motion_notification(notification):
    """Return motion state and durable data, including cleared notifications."""
    element = _message_element(notification)
    xml = _xml_details(element) if element is not None else None
    raw = _serialized(notification)
    topic = _topic(raw)
    if not topic:
        topic = next((str(item) for item in _walk(raw) if isinstance(item, str) and "motion" in item.lower()), "")
    items = xml["items"] if xml else _simple_items(raw)
    motion_values = [value for name, value in items.items() if "motion" in name.lower()]
    if "motion" not in topic.lower() and not motion_values:
        return None
    if not motion_values: return None
    active = any(str(value).strip().lower() in {"true", "1", "yes", "on", "active"}
                 for value in motion_values)
    occurred = (xml or {}).get("utc_time") or next((item.get("UtcTime") for item in _walk(raw)
                     if isinstance(item, dict) and item.get("UtcTime")), None)
    if isinstance(occurred, datetime):
        occurred = occurred if occurred.tzinfo else occurred.replace(tzinfo=timezone.utc)
    elif occurred:
        try: occurred = parse_utc(str(occurred))
        except ValueError: occurred = utcnow()
    else:
        occurred = utcnow()
    metadata = json.loads(json.dumps(
        {"topic": topic or "onvif_motion", "items": items,
         "property_operation": (xml or {}).get("property_operation"),
         "message_xml": (xml or {}).get("xml"), "notification": None if xml else raw}, default=str
    ))
    identity = json.dumps(metadata, sort_keys=True, separators=(",", ":"), default=str)
    source_items = {name: value for name, value in items.items() if "motion" not in name.lower()}
    source_identity = json.dumps({"topic": topic or "onvif_motion", "items": source_items},
                                 sort_keys=True, separators=(",", ":"), default=str)
    return active, occurred, metadata, hashlib.sha256(identity.encode()).hexdigest(), hashlib.sha256(source_identity.encode()).hexdigest()


def normalize_motion_notification(notification):
    """Return an ingestible active motion event or None."""
    parsed = parse_motion_notification(notification)
    if parsed is None or not parsed[0]: return None
    _active, occurred, metadata, event_id, _source_id = parsed
    return occurred, metadata, event_id


class OnvifEventWorker:
    def __init__(self, camera, repository, gateway, pull_timeout="PT5S", message_limit=32,
                 cooldown_seconds=20.0, clock=time.monotonic):
        self.camera, self.repository, self.gateway = camera, repository, gateway
        self.pull_timeout, self.message_limit = pull_timeout, message_limit
        self.cooldown_seconds, self.clock = cooldown_seconds, clock
        self._active_sources, self._last_trigger_at = set(), {}
        self._stop = threading.Event()
        self._ingest_lock = threading.Lock()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"onvif-events-{self.camera.id}", daemon=True)
        self._thread.start()

    def _handle_notification(self, notification):
        with self._ingest_lock:
            if self._stop.is_set(): return
            parsed = parse_motion_notification(notification)
            if parsed is None: return
            active, occurred, metadata, event_id, source_id = parsed
            if not active:
                self._active_sources.discard(source_id)
                return
            if source_id in self._active_sources: return
            self._active_sources.add(source_id)
            now = self.clock()
            if now - self._last_trigger_at.get(source_id, float("-inf")) < self.cooldown_seconds:
                return
            self._last_trigger_at[source_id] = now
            self.repository.accept_event(self.camera.id, event_id, "onvif_motion", occurred, metadata)

    def _run(self):
        while not self._stop.is_set():
            try:
                pullpoint = self.gateway.pullpoint(self.camera.onvif_endpoint, self.camera.onvif_username,
                                                   self.camera.onvif_password)
                while not self._stop.is_set():
                    response = pullpoint.PullMessages({"Timeout": self.pull_timeout, "MessageLimit": self.message_limit})
                    if self._stop.is_set(): return
                    notifications = getattr(response, "NotificationMessage", None)
                    if notifications is None:
                        serialized = _serialized(response)
                        notifications = serialized.get("NotificationMessage", []) if isinstance(serialized, dict) else []
                    for notification in notifications or []:
                        if self._stop.is_set(): return
                        self._handle_notification(notification)
            except Exception as exc:
                if self._stop.is_set(): return
                logger.warning("ONVIF event subscription will reconnect for camera %s: %s",
                               self.camera.id, exc)
                self._stop.wait(1)

    def stop(self, timeout=6):
        self._stop.set()
        with self._ingest_lock: pass
        if self._thread: self._thread.join(timeout)


class OnvifEventManager:
    def __init__(self, camera_repository, event_repository, gateway, worker_factory=OnvifEventWorker,
                 cooldown_seconds=20.0):
        self.camera_repository, self.event_repository = camera_repository, event_repository
        self.gateway, self.worker_factory = gateway, worker_factory
        self.cooldown_seconds = cooldown_seconds
        self._workers = {}
        self._lock = threading.Lock()

    def start(self, camera_id):
        camera = self.camera_repository.get(camera_id)
        if not (camera.enabled and camera.source_type == "onvif" and camera.onvif_endpoint): return
        with self._lock:
            if camera_id in self._workers: return
            try:
                worker = self.worker_factory(camera, self.event_repository, self.gateway,
                                             cooldown_seconds=self.cooldown_seconds)
            except TypeError:
                worker = self.worker_factory(camera, self.event_repository, self.gateway)
            self._workers[camera_id] = worker
        worker.start()

    def start_enabled(self):
        for camera in self.camera_repository.list(): self.start(camera.id)

    def stop(self, camera_id):
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if worker: worker.stop()

    def shutdown(self):
        with self._lock:
            workers, self._workers = list(self._workers.values()), {}
        for worker in workers: worker.stop()
