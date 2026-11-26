"""EGAP → 独立虚拟来源 HTTP；每次读取重新经过服务端对象授权，不信任本地缓存。

实例地址、身份和租户来自操作员私有配置；工具参数只选择对象和固定版本。
原件作为数据返回，不能把正文中的指令转化为工具权限或自动公司批准。
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import os
import stat
import threading
from urllib.parse import urlsplit


class SourceClientError(ValueError):
    def __init__(self,code:str,status:int|None=None):
        self.code,self.status=code,status
        super().__init__(code)


class SourceClient:
    def __init__(self,config:dict):
        self.config=config
        endpoint=urlsplit(config["base_url"])
        if endpoint.scheme!="http" or endpoint.hostname!="127.0.0.1" or not endpoint.port or endpoint.path not in ("","/") \
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment \
                or config["source_kind"]!="SIMULATED_ENTERPRISE_SOURCE" \
                or not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}",config["token"]):
            raise SourceClientError("REGISTERED_LOCAL_SOURCE_REQUIRED")
        self.port=endpoint.port

    @classmethod
    def from_file(cls,path:Path):
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>16384:
                raise SourceClientError("PRIVATE_READER_CONFIG_REQUIRED")
            return cls(json.load(stream))

    def request(self,method:str,path:str,body:dict|None=None)->dict:
        """总时限、有界响应、无代理/重定向/重试；POST 缺回执必须按原 publication_id 对账。"""
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=3)
        def abort():
            if conn.sock:
                try:conn.sock.shutdown(2)
                except OSError:pass
            conn.close()
        timer=threading.Timer(5,abort);timer.daemon=True;timer.start()
        try:
            headers={"Authorization":"Bearer "+self.config["token"],"Accept":"application/json"}
            if body is not None:headers["Content-Type"]="application/json"
            conn.request(method,path,None if body is None else json.dumps(body),headers)
            response=conn.getresponse();raw=response.read(1_048_577)
            if len(raw)>1_048_576 or response.getheader("Content-Type")!="application/json":
                raise SourceClientError("SOURCE_RESPONSE_UNCONFIRMED")
            value=json.loads(raw)
            if not isinstance(value,dict):raise SourceClientError("SOURCE_RESPONSE_UNCONFIRMED")
            if response.status not in (200,201):
                code=value.get("error_code")
                if not isinstance(code,str) or not re.fullmatch(r"[A-Z_]{1,80}",code):code="SOURCE_REQUEST_REJECTED"
                raise SourceClientError(code,response.status)
            return value
        except (OSError,http.client.HTTPException,ValueError) as error:
            if isinstance(error,SourceClientError):raise
            raise SourceClientError("PUBLICATION_UNKNOWN" if method=="POST" else "SOURCE_READ_UNAVAILABLE") from None
        finally:timer.cancel();conn.close()

    @staticmethod
    def path(namespace:str,object_id:str,version:str)->str:
        if namespace not in {"rules","acceptance","jira","diagnostics","providers"} or any(not isinstance(v,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",v) for v in (object_id,version)):
            raise SourceClientError("SOURCE_OBJECT_IDENTITY_INVALID")
        return f"/v1/objects/{namespace}/{object_id}/versions/{version}"

    def read(self,namespace:str,object_id:str,version:str,*,expected_sha256:str|None=None)->dict:
        """核对来源实例、租户、原件摘要与准确版本；新鲜旧内容也不能跳过网络授权。"""
        result=self.request("GET",self.path(namespace,object_id,version))
        expected={"schema_version":1,"instance_id":self.config["instance_id"],"tenant_id":self.config["tenant_id"],
            "namespace":namespace,"object_id":object_id,"version":version,"source_kind":"SIMULATED_ENTERPRISE_SOURCE"}
        if any(result.get(k)!=v for k,v in expected.items()):raise SourceClientError("SOURCE_RESPONSE_IDENTITY_MISMATCH")
        acl=result.get("acl_decision",{})
        if not isinstance(acl,dict) or acl.get("principal_id")!=self.config["principal_id"] or acl.get("permission")!="READ" or acl.get("scope")!="EXACT_OBJECT_AND_TENANT" or acl.get("cache_authorization_seconds")!=0:
            raise SourceClientError("SOURCE_AUTHORIZATION_UNCONFIRMED")
        try:raw=base64.b64decode(result["content_base64"],validate=True);content=raw.decode("utf-8")
        except (KeyError,TypeError,ValueError,UnicodeError):raise SourceClientError("SOURCE_CONTENT_INVALID") from None
        digest=hashlib.sha256(raw).hexdigest()
        if digest!=result.get("content_sha256") or (expected_sha256 is not None and digest!=expected_sha256):
            raise SourceClientError("SOURCE_CONTENT_DIGEST_MISMATCH")
        if not isinstance(result.get("metadata"),dict) or result["metadata"].get("authority_scope")!="SYNTHETIC_LOCAL_CALIBRATION":
            raise SourceClientError("SOURCE_AUTHORITY_UPGRADE_FORBIDDEN")
        return {**result,"content":content}
