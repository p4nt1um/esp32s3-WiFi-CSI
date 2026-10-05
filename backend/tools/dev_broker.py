"""本地开发 broker（amqtt，无系统依赖）。生产环境用 VPS 上的 Mosquitto(TLS)。"""
import asyncio

from amqtt.broker import Broker

CONFIG = {
    "listeners": {
        "default": {"type": "tcp", "bind": "0.0.0.0:11883"},   # 1883 在本机 Hyper-V 保留段内
    },
    "sys_interval": 10,
    "auth": {"allow-anonymous": True},
}


async def main() -> None:
    broker = Broker(CONFIG)
    await broker.start()
    print("amqtt broker on 0.0.0.0:11883 (Ctrl+C 退出)")
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
