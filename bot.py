from wechat.utils import get_processes

from wechat import WeChat, events


def detach_wechat():
    try:
        processes = get_processes("hook.exe")
        if processes:
            for process in processes:
                process.kill()
    except Exception:
        pass


detach_wechat()

wechat = WeChat()
wechat.open()

@wechat.handle(events.TEXT_MESSAGE)
def _(bot, event):
    print(event)

wechat.run()
