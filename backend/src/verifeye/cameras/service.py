"""Camera use cases; independent from FastAPI and transport details."""

from .models import InvalidCameraConfiguration
from .security import validate_rtsp_url


class CameraService:
    def __init__(self, repository, manager): self.repository, self.manager = repository, manager
    def list(self): return [(camera, self.manager.status(camera.id)) for camera in self.repository.list()]
    def create(self, name, url, enabled=True, source_type="manual"):
        if not name or not name.strip(): raise InvalidCameraConfiguration("Camera name is required.")
        if source_type not in {"manual", "onvif"}: raise InvalidCameraConfiguration("Camera source must be manual or onvif.")
        try: url = validate_rtsp_url(url)
        except ValueError as exc: raise InvalidCameraConfiguration(str(exc)) from exc
        camera = self.repository.create(name, url, enabled, source_type)
        if enabled: self.manager.start(camera.id)
        return camera, self.manager.status(camera.id)
    def update(self, camera_id, *, name=None, url=None, enabled=None):
        if url is not None:
            try: url = validate_rtsp_url(url)
            except ValueError as exc: raise InvalidCameraConfiguration(str(exc)) from exc
        old = self.repository.get(camera_id)
        camera = self.repository.update(camera_id, name=name, url=url, enabled=enabled)
        connection_changed = url is not None and url != old.url
        if not camera.enabled: self.manager.stop(camera_id)
        elif connection_changed: self.manager.restart(camera_id)
        elif enabled is True and not self.manager.status(camera_id).running: self.manager.start(camera_id)
        return camera, self.manager.status(camera_id)
    def delete(self, camera_id): self.manager.delete(camera_id)
    def start(self, camera_id): return self.manager.start(camera_id)
    def stop(self, camera_id): return self.manager.stop(camera_id)


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
        camera = ONVIFCamera(parsed.hostname, parsed.port or 80, username, password)
        media = camera.create_media_service(); result = []
        for profile in media.GetProfiles():
            uri = media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}, "ProfileToken": profile.token}).Uri
            result.append({"token": profile.token, "name": getattr(profile, "Name", profile.token), "uri": uri})
        return result
