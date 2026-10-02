# 本机微信读取层：第三方来源

`wr/*.py` 是 WechatVibe 自行实现的只读读取层，由 `bridge/live_source.py` 导入。当前 WechatVibe 便携版把这些 Python 源文件与锁定 Python 3.14 运行时放在 `resources/client/`；这里不使用旧 PyInstaller 读取器可执行文件。

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| Python 3.14 | 本地读取和服务运行时 | PSF License，运行时 `LICENSE.txt` |
| `cryptography` 46.0.3 | SQLCipher 页面解密 | Apache-2.0 OR BSD-3-Clause，安装包内许可证 |
| `zstandard` 0.25.0 | 压缩正文解压 | BSD-3-Clause，安装包内许可证 |
| `psutil` 7.1.3 | 只读进程发现 | BSD-3-Clause，安装包内许可证 |
| `wechatauto-replica` 1.2.2.6 | 本地数据库读取接口 | Apache-2.0，安装发行包 `dist-info/licenses/LICENSE` |

完整 Python 运行依赖版本见根目录 `python-requirements.lock.txt`，源码和模型的其他许可见根目录 `THIRD_PARTY_NOTICES.md`。

读取层不发送消息、不点击微信界面、不注入 DLL、不写入微信进程内存，也不改动微信原始数据库。活动账号通过本机只读文件占用信息确定；无法确定唯一账号时不借用旧账号结果。运行产生的账号快照和分析结果属于用户数据，不在源码包中。
