"""輸入轉接器。"""
from .base import InputAdapter
from .keyboard import KeyboardInput
from .stubs import ButtonInput, TouchInput, VisionInput
from .web_remote import WebRemoteInput

__all__ = ["InputAdapter", "KeyboardInput", "WebRemoteInput", "TouchInput", "ButtonInput", "VisionInput"]
