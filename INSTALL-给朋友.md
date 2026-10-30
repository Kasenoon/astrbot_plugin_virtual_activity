# 安装说明（给朋友）

这是一个 AstrBot 插件：**虚拟活动**。机器人有一间平面房子，家具就是互动按钮。

## 1. 环境要求

- AstrBot **≥ 4.20**（开发和实测用的是 **4.29.1**）
- 不需要额外 pip 包，不需要 Node、不需要编译

## 2. 安装

把压缩包里的 `astrbot_plugin_virtual_activity` 整个文件夹解压到：

```text
<你的 AstrBot 目录>/data/plugins/
```

最终结构应该是：

```text
data/plugins/astrbot_plugin_virtual_activity/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ core/
├─ views/
└─ README.md
```

然后在 WebUI → 插件 → 找到「虚拟活动」→ 重载/启用。首次加载会自动生成示例房间
（床 / 书桌 / 窗边 / 沙发 / 茶几 / 冰箱 / 电视 / 绿植）。

## 3. 必改的配置

| 配置项 | 说明 |
|---|---|
| `admin_qq` | **必改**：你自己的 QQ，活动消息和指令权限都绑这个号 |
| `weather_key` / `weather_api_host` | 可选：不填就不显示天气，其他功能照常 |
| `tv_cookie` | 可选：不填也能看 B 站，填了更不容易被风控 |

其他保持默认即可。

## 4. 试一试

私聊机器人：

```text
/回家          看它现在在干嘛
/测试点 沙发    强制触发一次（绕过所有限制，方便测试）
/测试tick      随机触发一次
/虚拟活动      帮助
```

WebUI → 插件 → 虚拟活动 → 打开「房间」视图，可以拖动摆家具、点家具触发、
上传机器人图标（可拖动缩放取景）。

## 5. 数据在哪

```text
data/plugin_data/astrbot_plugin_virtual_activity/
├─ room.json           # 房间布局（家具、坐标、效果）
├─ state_default.json  # 机器人状态（位置、精力、社交、电视配额）
└─ runtime.json        # 记住的管理员会话
```

删掉整个目录就会回到默认示例房间。

## 注意事项

- 默认**不报备**：自动活动只更新状态，不发消息；想让它说话就把 `announce_activity` 打开
- 电视只在「谷时」可选：工作日 12:00-14:00、18:00-22:00，周末全天（可配）
- 完整配置项和机制说明见 `README.md`
