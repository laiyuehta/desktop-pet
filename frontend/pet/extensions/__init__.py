"""能力擴充接口（第二期）。"""
from .base import ACTIONS, Extension, ExtensionManager
from .bus import EventBus
from .camera import CameraSnapshot, CameraWebAPI, PhotoUploader
from .example import ExampleExtension

__all__ = ["EventBus", "Extension", "ExtensionManager", "ExampleExtension", "ACTIONS",
           "CameraSnapshot", "CameraWebAPI", "PhotoUploader"]
