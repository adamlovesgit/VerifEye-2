"""Camera use cases; independent from FastAPI and transport details."""

from functools import lru_cache
from importlib.metadata import distribution
from pathlib import Path

from .models import InvalidCameraConfiguration
from .security import validate_rtsp_url


@lru_cache(maxsize=1)
def onvif_wsdl_dir() -> Path:
    """Resolve onvif-zeep data files without relying on its broken user-install default."""
    package = distribution("onvif-zeep")
    for entry in package.files or ():
        normalized = str(entry).replace("\\", "/")
        if normalized.endswith("wsdl/devicemgmt.wsdl"):
            candidate = Path(package.locate_file(entry)).resolve().parent
            required = ("devicemgmt.wsdl", "media.wsdl", "events.wsdl")
            if all((candidate / name).is_file() for name in required):
                return candidate
    raise RuntimeError("onvif-zeep is installed without its required WSDL files.")


class CameraService:
    def __init__(self, repository, manager, onvif_events=None):
        self.repository, self.manager = repository, manager
        self.onvif_events = onvif_events
    def list(self): return [(camera, self.manager.status(camera.id)) for camera in self.repository.list()]
    def create(self, name, url, enabled=True, source_type="manual", recognition_url=None,
               onvif_endpoint=None, onvif_username=None, onvif_password=None):
        if not name or not name.strip(): raise InvalidCameraConfiguration("Camera name is required.")
        if source_type not in {"manual", "onvif"}: raise InvalidCameraConfiguration("Camera source must be manual or onvif.")
        try: url = validate_rtsp_url(url)
        except ValueError as exc: raise InvalidCameraConfiguration(str(exc)) from exc
        if recognition_url:
            try: recognition_url = validate_rtsp_url(recognition_url)
            except ValueError as exc: raise InvalidCameraConfiguration(str(exc)) from exc
        camera = self.repository.create(name, url, enabled, source_type, recognition_url,
                                        onvif_endpoint, onvif_username, onvif_password)
        if enabled: self.manager.start(camera.id)
        if enabled and self.onvif_events: self.onvif_events.start(camera.id)
        return camera, self.manager.status(camera.id)
    def update(self, camera_id, *, name=None, url=None, enabled=None, recognition_url=...):
        if url is not None:
            try: url = validate_rtsp_url(url)
            except ValueError as exc: raise InvalidCameraConfiguration(str(exc)) from exc
        if recognition_url is not ... and recognition_url:
            try: recognition_url = validate_rtsp_url(recognition_url)
            except ValueError as exc: raise InvalidCameraConfiguration(str(exc)) from exc
        old = self.repository.get(camera_id)
        camera = self.repository.update(camera_id, name=name, url=url, enabled=enabled, recognition_url=recognition_url)
        connection_changed = (url is not None and url != old.url) or (
            recognition_url is not ... and recognition_url != old.recognition_url
        )
        if not camera.enabled:
            self.manager.stop(camera_id)
            if self.onvif_events: self.onvif_events.stop(camera_id)
        elif connection_changed: self.manager.restart(camera_id)
        elif enabled is True and not self.manager.status(camera_id).running: self.manager.start(camera_id)
        if camera.enabled and self.onvif_events: self.onvif_events.start(camera_id)
        return camera, self.manager.status(camera_id)
    def delete(self, camera_id):
        if self.onvif_events: self.onvif_events.stop(camera_id)
        self.manager.delete(camera_id)
    def start(self, camera_id):
        status = self.manager.start(camera_id)
        if self.onvif_events: self.onvif_events.start(camera_id)
        return status
    def stop(self, camera_id):
        if self.onvif_events: self.onvif_events.stop(camera_id)
        return self.manager.stop(camera_id)


class OnvifGateway:
    """Optional ONVIF adapter. Discovery occurs only through this explicit method."""
    def discover(self, timeout=5):
        from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery
        from wsdiscovery import QName
        discovery = WSDiscovery(); discovery.start()
        try:
            services = discovery.searchServices(types=[QName("http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter")], timeout=timeout)
            return [{"endpoint": address} for service in services for address in service.getXAddrs()]
        finally: discovery.stop()
    def profiles(self, endpoint, username, password):
        from urllib.parse import urlsplit
        from onvif import ONVIFCamera
        parsed = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        camera = ONVIFCamera(
            parsed.hostname, parsed.port or 80, username, password,
            wsdl_dir=str(onvif_wsdl_dir()), no_cache=True,
        )
        media = camera.create_media_service(); result = []
        for profile in media.GetProfiles():
            uri = media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}, "ProfileToken": profile.token}).Uri
            result.append({"token": profile.token, "name": getattr(profile, "Name", profile.token), "uri": uri})
        return result

    def pullpoint(self, endpoint, username, password):
        """Create the camera's PullPoint subscription service."""
        from urllib.parse import urlsplit
        from onvif import ONVIFCamera
        parsed = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        camera = ONVIFCamera(
            parsed.hostname, parsed.port or 80, username, password,
            wsdl_dir=str(onvif_wsdl_dir()), no_cache=True,
        )
        subscription = camera.create_events_service().CreatePullPointSubscription()
        address = subscription.SubscriptionReference.Address
        address = getattr(address, "_value_1", address)
        if not address:
            raise RuntimeError("The ONVIF camera returned an empty PullPoint subscription address.")
        camera.xaddrs[
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
        ] = str(address)
        return camera.create_pullpoint_service()
