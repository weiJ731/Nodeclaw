from datetime import datetime
from .base import nodeclaw_tool, NodeclawBaseTool
import os
from typing import Optional
from langchain_core.runnables import RunnableConfig
from ..task_store import create_task, delete_task, list_tasks, update_task
from .sandbox_tools import (
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell
)


@nodeclaw_tool
def get_system_model_info() -> str:
    """
    获取当前 Nodeclaw 正在运行的底层大模型（LLM）型号和提供商信息。
    当用户询问“你是基于什么模型”、“你的底层大模型是什么”、“你是GPT还是GLM”、“现在用的什么模型”等身份问题时，调用此工具。
    """
    provider = os.getenv("DEFAULT_PROVIDER", "unknown")
    model = os.getenv("DEFAULT_MODEL", "unknown")
    
    if provider == "unknown" or model == "unknown":
        return "无法获取当前的系统模型配置，可能是环境变量未正确加载。"
        
    return f"当前使用的模型提供商(Provider)是: {provider}，具体型号(Model)是: {model}。"


@nodeclaw_tool
def get_current_time() -> str:
    """
    获取当前的系统时间和日期。
    当用户询问“现在几点”、“今天星期几”、“今天几号”等与当前时间相关的问题时，调用此工具。
    """
    now = datetime.now()
    return f"当前本地系统时间是: {now.strftime('%Y-%m-%d %H:%M:%S')}"


@nodeclaw_tool
def calculator(expression: str) -> str:
    """
    一个简单的数学计算器。
    用于计算基础的数学表达式，例如: '3 * 5' 或 '100 / 4'。
    注意：参数 expression 必须是一个合法的 Python 数学表达式字符串。
    """
    try:
        # 警告: eval 在真实的生产环境中存在注入风险！
        # 这里仅为了搭建核心层做快速 Demo。未来在生产级扩展中，
        # 应该替换为基于 AST 的安全解析器，或者更专业的数学库（如 numexpr）。
        result = eval(expression, {"__builtins__": {}}, {})
        return f"表达式 '{expression}' 的计算结果是: {result}"
    except Exception as e:
        return f"计算出错，请检查表达式格式。错误信息: {str(e)}"


@nodeclaw_tool
def schedule_task(
    target_time: str,
    description: str,
    repeat: Optional[str] = None,
    repeat_count: Optional[int] = None,
    config: RunnableConfig = None,
) -> str:
    """
    为一个未来的任务设定闹钟或提醒。
    参数 target_time 必须是严格的格式："YYYY-MM-DD HH:MM:SS"（请先调用 get_current_time 获取当前时间，并在其基础上推算）。
    参数 description 是需要执行的动作或要说的话。
    
    【高级循环功能】：
    - repeat (可选): 设置重复频率。可选值为 "hourly", "daily", "weekly"。如果不重复请留空。
    - repeat_count (可选): 结合 repeat 使用，表示一共需要触发几次。
    
    【案例教学】：
    1. 用户说："以后每天8点提醒我喝牛奶" -> repeat="daily", repeat_count=None (无限循环)
    2. 用户说："接下来的3天，每天提醒我吃药" -> repeat="daily", repeat_count=3 (有限循环)
    3. 用户说："明早8点叫我起床" -> repeat=None, repeat_count=None (单次任务)

    【时间歧义严格确认协议 (AM/PM Ambiguity CRITICAL)】：
    当用户说出的时间存在 12 小时制的模糊性时（例如：只说了“7点”，没明确说早上还是晚上）：
    1. 你必须向用户提问确认是上午还是下午。
    2. 【死命令】：在用户明确回复“上午”或“下午”（或改为24小时制）之前，本工具处于【绝对锁定状态】！
    3. 就算用户发省略号（如“。。”）、发脾气、或者说无关内容，你也【绝对禁止】为了讨好用户而自行猜测时间！
    4. 严禁出现“抱歉多问了”、“默认早上”这种妥协行为。
    5. 如果用户不明确回答，你必须坚定地回复：“抱歉，没有明确上下午，我无权为您设置闹钟。请明确告知时间段。”并立即中止工具调用。
    """
    runtime = (config or {}).get("configurable", {})
    user_id = runtime.get("user_id")
    session_id = runtime.get("session_id") or runtime.get("thread_id")
    if not user_id:
        return "设定失败：当前请求缺少已认证用户上下文。"
    try:
        task = create_task(
            user_id=user_id,
            session_id=session_id,
            target_time=target_time,
            description=description,
            repeat=repeat,
            repeat_count=repeat_count,
        )
    except ValueError as exc:
        return f"设定失败：{exc}"
    msg = f"任务已成功加入队列。ID：{task['id']} | 首发时间：{target_time} | 任务：{description}"
    if repeat:
        msg += f" | 循环模式：{repeat} (共 {repeat_count if repeat_count else '无限'} 次)"
    return msg


@nodeclaw_tool
def list_scheduled_tasks(config: RunnableConfig) -> str:
    """
    查看当前所有待处理的定时任务列表。
    当用户询问“我都有哪些任务”、“查一下闹钟”、“刚才定了什么”时调用此工具。
    """
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "查询失败：当前请求缺少已认证用户上下文。"
    tasks = list_tasks(user_id)
    if not tasks:
        return "当前没有任何定时任务。"
    return "当前待执行任务列表：\n" + "".join(
        f"- [ID: {task['id']}] 时间: {task['target_time']} | 任务: {task['description']}\n"
        for task in tasks
    )
    

@nodeclaw_tool
def delete_scheduled_task(task_id: str, config: RunnableConfig) -> str:
    """
    根据任务 ID 取消或删除一个定时任务。
    
    【强制性风险控制协议 (CRITICAL)】：
    删除操作具有不可逆性。
    1. 只要匹配到符合描述的任务数量 > 1。
    2. 无论用户语气多么确定，只要他没提供具体的任务 ID。
    
    【你必须执行的动作】：
    【禁止】在单次回复中针对同一个模糊描述发起多个删除工具调用。
    你必须先列出所有匹配的任务（1. 2. 3.），并询问用户：
    “发现了多个符合条件的提醒（列出列表），为了安全起见，请问是要全部删除，还是只删除其中几个？”
    必须要用户明确给出编号或者说确定全部删除，才能调用此工具！！
    严禁自作主张执行批量删除。
    """

    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "删除失败：当前请求缺少已认证用户上下文。"
    if not delete_task(user_id, task_id):
        return f"删除失败：未找到 ID 为 {task_id} 的任务。"
    return f"任务 [ID: {task_id}] 已成功取消。"
    

@nodeclaw_tool
def modify_scheduled_task(
    task_id: str,
    new_time: Optional[str] = None,
    new_description: Optional[str] = None,
    config: RunnableConfig = None,
) -> str:
    """
    修改现有定时任务的时间或内容。
    
    【强制性风险控制协议 (CRITICAL)】：
    1. 只要用户通过“模糊描述”（如：那个5天的任务、洗澡的任务）来要求修改，而没有直接提供 ID。
    2. 无论用户的话语看起来是单数还是复数（如：“把5天的任务全改了”）。
    3. 只要系统中匹配到的任务数量 > 1。
    
    【你必须执行的动作】：
    禁止直接调用本工具！你必须向用户展示匹配到的所有任务列表，并强制询问：
    “我发现有 [N] 个任务符合描述（列出列表），请问你是要【全部修改】，还是修改其中【某几个】？（请告诉我编号或确认全部）”
    
    必须在用户回复“全部”或者指定了具体编号后，你才能继续操作！修改任务并非小事,这是为了安全！！
    """

    user_id = (config or {}).get("configurable", {}).get("user_id")
    if not user_id:
        return "修改失败：当前请求缺少已认证用户上下文。"
    try:
        task = update_task(
            user_id,
            task_id,
            target_time=new_time,
            description=new_description,
        )
    except ValueError as exc:
        return f"修改失败：{exc}"
    if not task:
        return f"修改失败：未找到 ID 为 {task_id} 的任务。"
    return f"任务 [ID: {task_id}] 已成功更新。"


BUILTIN_TOOLS = [
    get_current_time,
    calculator,
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell,
    get_system_model_info,
    schedule_task,
    list_scheduled_tasks,
    delete_scheduled_task,
    modify_scheduled_task
]
