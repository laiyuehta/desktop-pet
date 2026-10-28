"""LLM 路由：決定一個請求交給本機小模型還是雲端。

第一期：永遠走本機（local_only）。雲端只有接口（CloudProvider），預設關閉、沒有金鑰。
之後可以在 Router.classify() 依任務複雜度（長文、查資料、工具呼叫……）改走雲端。
"""

from __future__ import annotations

import abc
import os
from typing import Any, Dict, List, Optional

from .llm import LLMError, OpenAICompatClient


class Provider(abc.ABC):
    name = "provider"

    @abc.abstractmethod
    def chat(self, messages: List[Dict[str, str]]) -> str:
        """回傳模型文字，失敗丟 LLMError。"""

    def ping(self) -> Dict[str, Any]:
        return {"reachable": False}

    @property
    def available(self) -> bool:
        return True


class LocalProvider(Provider):
    """本機 OpenAI 相容服務（R7 上的 llama-server；之後換 1~3B 小模型只改設定）。"""
    name = "local"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.client = OpenAICompatClient.from_config(cfg)

    def chat(self, messages):
        return self.client.chat(messages)

    def ping(self):
        r = self.client.ping()
        r["model"] = self.client.model
        r["base_url"] = self.client.base_url
        return r


class CloudProvider(Provider):
    """雲端 API（第二期）。TODO：選定供應商後實作；金鑰從環境變數讀，不寫進設定檔。"""
    name = "cloud"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", False))

    @property
    def available(self) -> bool:
        return self.enabled and bool(os.environ.get(self.cfg.get("api_key_env", ""), ""))

    def chat(self, messages):
        raise LLMError("雲端 provider 尚未實作（第二期）")

    def ping(self):
        return {"reachable": False, "enabled": self.enabled, "note": "尚未實作"}


class Router:
    def __init__(self, llm_cfg: Dict[str, Any], local: Optional[Provider] = None,
                 cloud: Optional[Provider] = None):
        self.mode = llm_cfg.get("router", {}).get("mode", "local_only")
        self.local = local or LocalProvider(llm_cfg["local"])
        self.cloud = cloud or CloudProvider(llm_cfg.get("cloud", {}))

    def classify(self, task: Dict[str, Any]) -> str:
        """回傳 "simple" 或 "complex"。TODO：之後依任務內容判斷；目前寵物閒聊都算 simple。"""
        return task.get("complexity", "simple")

    def pick(self, task: Dict[str, Any]) -> Provider:
        if self.mode != "local_only" and self.classify(task) == "complex" and self.cloud.available:
            return self.cloud
        return self.local
