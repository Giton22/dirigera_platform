from __future__ import annotations
from typing import Any, Dict, List, Optional
from typing import Any, Optional, Dict
import re

from dirigera import Hub

from dirigera.devices.device import Attributes, Device
from dirigera.hub.abstract_smart_home_hub import AbstractSmartHomeHub
from dirigera.devices.scene import Info, Icon,  SceneType, Trigger, TriggerDetails, ControllerType
import logging 

logger = logging.getLogger("custom_components.dirigera_platform")


# Environment sensor patch for ALPSTUGA (adds current_c_o2 support)
# The dirigera library doesn't have current_c_o2 field yet
class EnvironmentSensorAttributesX(Attributes):
    current_temperature: Optional[float] = None
    current_r_h: Optional[int] = None
    current_p_m25: Optional[int] = None
    max_measured_p_m25: Optional[int] = None
    min_measured_p_m25: Optional[int] = None
    voc_index: Optional[int] = None
    battery_percentage: Optional[int] = None
    current_c_o2: Optional[int] = None  # Added for ALPSTUGA CO2 sensor


class EnvironmentSensorX(Device):
    dirigera_client: AbstractSmartHomeHub
    attributes: EnvironmentSensorAttributesX

    def reload(self) -> "EnvironmentSensorX":
        data = self.dirigera_client.get(route=f"/devices/{self.id}")
        return EnvironmentSensorX(dirigeraClient=self.dirigera_client, **data)

    def set_name(self, name: str) -> None:
        if "customName" not in self.capabilities.can_receive:
            raise AssertionError("This sensor does not support the set_name function")
        data = [{"attributes": {"customName": name}}]
        self.dirigera_client.patch(route=f"/devices/{self.id}", data=data)
        self.attributes.custom_name = name


def dict_to_environment_sensor_x(
    data: Dict[str, Any], dirigera_client: AbstractSmartHomeHub
) -> EnvironmentSensorX:
    return EnvironmentSensorX(dirigeraClient=dirigera_client, **data)


# Patch to fix issues with motion sensor
class HubX(Hub):
    def __init__(
        self, token: str, ip_address: str, port: str = "8443", api_version: str = "v1"
    ) -> None:
        super().__init__(token, ip_address, port, api_version)

    def get_controllers(self) -> List[ControllerX]:
        """
        Fetches all controllers registered in the Hub
        """
        devices = self.get("/devices")
        controllers = list(filter(lambda x: x["type"] == "controller", devices))
        return [dict_to_controller(controller, self) for controller in controllers]
    
    # Scenes are a problem so making a hack
    def get_scenes(self):
        """
        Fetches all controllers registered in the Hub
        """
        scenes = self.get("/scenes")
        #scenes = list(filter(lambda x: x["type"] == "scene", devices))
        
        return [HackScene.make_scene(self, scene) for scene in scenes]
    
    def get_scene_by_id(self, scene_id: str):
        """
        Fetches a specific scene by a given id
        """
        data = self.get(f"/scenes/{scene_id}")
        return HackScene.make_scene(self, data)
    
    def create_empty_scene(self, controller_id: str, clicks_supported: list, number_of_buttons: int = 1):
        """Create empty scenes used only as event generators.

        Why: Dirigera's websocket events are inconsistent across controller models.
        Some remotes (e.g. STYRBAR) send ambiguous `remotePressEvent` payloads.
        Creating scenes with per-button triggers makes the hub emit `sceneUpdated`
        with a `buttonIndex`, which we can map to `buttonX_*` events in HA.

        We always create a legacy shortcutController trigger with buttonIndex=0
        for backward compatibility.

        For multi-button controllers we additionally create lightController
        triggers with buttonIndex=1..N, but only for the *primary* controller id
        (no suffix, or suffix `_1`). This avoids creating redundant scenes for
        secondary ids like `_2`, `_3`, ... that some controllers expose.
        """

        try:
            number_of_buttons = int(number_of_buttons) if number_of_buttons is not None else 1
        except Exception:
            number_of_buttons = 1

        logger.debug(
            f"Creating empty scene(s) for controller: {controller_id} clicks={clicks_supported} buttons={number_of_buttons}"
        )

        # Determine whether this controller_id is the primary id for multi-button creation.
        # Pattern matches ids ending in _N (N numeric). Only _1 is treated as primary.
        allow_multibutton = number_of_buttons > 1
        m = re.search(r"^(.*)_([0-9]+)$", controller_id)
        if m is not None and m.group(2) != "1":
            allow_multibutton = False

        def _post_empty_scene(click_pattern: str, controller_type: str, button_index: int) -> None:
            scene_name = (
                f"dirigera_integration_empty_scene_{controller_id}_{controller_type}_{button_index}_{click_pattern}"
            )
            data = {
                "info": {"name": scene_name, "icon": "scenes_cake"},
                "type": "customScene",
                "triggers": [
                    {
                        "type": "controller",
                        "disabled": False,
                        "trigger": {
                            "controllerType": controller_type,
                            "clickPattern": click_pattern,
                            "buttonIndex": button_index,
                            "deviceId": controller_id,
                        },
                    }
                ],
                "actions": [],
            }
            logger.debug(f"Creating empty scene: {scene_name}")
            self.post("/scenes/", data=data)

        for click in clicks_supported:
            # Legacy generator: works for shortcut controllers and id-suffixed controllers
            _post_empty_scene(click, "shortcutController", 0)

            # Multi-button generator: required for remotes that only expose per-button via buttonIndex.
            if allow_multibutton:
                for btn_idx in range(1, number_of_buttons + 1):
                    _post_empty_scene(click, "lightController", btn_idx)
        
    def delete_empty_scenes(self):
        scenes = self.get_scenes()
        for scene in scenes:
            if scene.name.startswith("dirigera_integration_empty_scene_"):
                logging.debug(f"Deleting Scene id: {scene.id} name: {scene.name}...")
                self.delete_scene(scene.id)

    def get_motion_sensors(self) -> List[MotionSensorX]:
        """
        Fetches all motion sensors registered in the Hub.
        Includes both motionSensor and occupancySensor device types.
        IKEA MYGGSPRAY sensors report as occupancySensor instead of motionSensor.
        """
        devices = self.get("/devices")
        sensors = list(filter(lambda x: x["deviceType"] in ("motionSensor", "occupancySensor"), devices))
        return [dict_to_motion_sensor_x(sensor, self) for sensor in sensors]

    def get_motion_sensor_by_id(self, id_: str) -> MotionSensorX:
        """
        Fetches a motion sensor by ID.
        Accepts both motionSensor and occupancySensor device types.
        """
        motion_sensor = self._get_device_data_by_id(id_)
        if motion_sensor["deviceType"] not in ("motionSensor", "occupancySensor"):
            raise ValueError("Device is not a MotionSensor or OccupancySensor")
        return dict_to_motion_sensor_x(motion_sensor, self)

    def get_environment_sensors(self) -> List[EnvironmentSensorX]:
        """
        Fetches all environment sensors registered in the Hub.
        Uses patched EnvironmentSensorX with current_c_o2 support for ALPSTUGA.
        """
        devices = self.get("/devices")
        sensors = list(filter(lambda x: x["deviceType"] == "environmentSensor", devices))
        return [dict_to_environment_sensor_x(sensor, self) for sensor in sensors]

    def get_environment_sensor_by_id(self, id_: str) -> EnvironmentSensorX:
        """
        Fetches an environment sensor by ID.
        Uses patched EnvironmentSensorX with current_c_o2 support for ALPSTUGA.
        """
        sensor = self._get_device_data_by_id(id_)
        if sensor["deviceType"] != "environmentSensor":
            raise ValueError("Device is not an EnvironmentSensor")
        return dict_to_environment_sensor_x(sensor, self)

class ControllerAttributesX(Attributes):
    is_on: Optional[bool] = None
    battery_percentage: Optional[int] = None
    switch_label: Optional[str] = None

class ControllerX(Device):
    dirigera_client: AbstractSmartHomeHub
    attributes: ControllerAttributesX

    def reload(self) -> ControllerX:
        data = self.dirigera_client.get(route=f"/devices/{self.id}")
        return ControllerX(dirigeraClient=self.dirigera_client, **data)

    def set_name(self, name: str) -> None:
        if "customName" not in self.capabilities.can_receive:
            raise AssertionError(
                "This controller does not support the set_name function"
            )

        data = [{"attributes": {"customName": name}}]
        self.dirigera_client.patch(route=f"/devices/{self.id}", data=data)
        self.attributes.custom_name = name

def dict_to_controller(
    data: Dict[str, Any], dirigera_client: AbstractSmartHomeHub
) -> ControllerX:
    return ControllerX(dirigeraClient=dirigera_client, **data)

class HackScene():

    def __init__(self, hub, id, name, icon):
        self.hub = hub
        self.id = id 
        self.name = name 
        self.icon = icon

    def parse_scene_json(json_data):
        id = json_data["id"]
        name = json_data["info"]["name"]
        icon = json_data["info"]["icon"]
        return id, name, icon 
    
    def make_scene(dirigera_client, json_data):
        id, name, icon = HackScene.parse_scene_json(json_data)
        return HackScene(dirigera_client, id, name, icon)
    
    def reload(self) -> HackScene:
        data = self.dirigera_client.get(route=f"/scenes/{self.id}")
        return HackScene.make_scene(self, data)
        #return Scene(dirigeraClient=self.dirigera_client, **data)

    def trigger(self) -> HackScene:
        self.hub.post(route=f"/scenes/{self.id}/trigger")

    def undo(self) -> HackScene:
        self.hub.post(route=f"/scenes/{self.id}/undo")


# Motion sensor patch for MYGGSPRAY (occupancySensor)
# MYGGSPRAY sensors don't have is_on attribute, so we make it optional
class MotionSensorAttributesX(Attributes):
    battery_percentage: Optional[int] = None
    is_on: Optional[bool] = None  # Made optional for MYGGSPRAY compatibility
    light_level: Optional[float] = None
    is_detected: Optional[bool] = False


class MotionSensorX(Device):
    dirigera_client: AbstractSmartHomeHub
    attributes: MotionSensorAttributesX

    def reload(self) -> "MotionSensorX":
        data = self.dirigera_client.get(route=f"/devices/{self.id}")
        return MotionSensorX(dirigeraClient=self.dirigera_client, **data)

    def set_name(self, name: str) -> None:
        if "customName" not in self.capabilities.can_receive:
            raise AssertionError("This sensor does not support the set_name function")
        data = [{"attributes": {"customName": name}}]
        self.dirigera_client.patch(route=f"/devices/{self.id}", data=data)
        self.attributes.custom_name = name


def dict_to_motion_sensor_x(
    data: Dict[str, Any], dirigera_client: AbstractSmartHomeHub
) -> MotionSensorX:
    return MotionSensorX(dirigeraClient=dirigera_client, **data)
