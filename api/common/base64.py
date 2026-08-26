#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""学习版 api/common/base64.py(与官方 v0.24.0 1:1 移植)。

init_superuser 写入超管密码时用的简单 base64 编码(非加密,仅混淆)。
官方文件:api/common/base64.py@v0.24.0
"""
import base64


def encode_to_base64(input_string: str) -> str:
    """将字符串做标准 base64 编码,返回 str(官方原样,无注释)。"""
    base64_encoded = base64.b64encode(input_string.encode('utf-8'))
    return base64_encoded.decode('utf-8')