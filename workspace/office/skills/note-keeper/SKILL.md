name: note_keeper
description: 管理 office 工位中的 Markdown 笔记与待办，支持追加笔记、搜索笔记、列出笔记文件和生成当日日报。

# Note Keeper

## 用途

当用户想把信息记录到 office 工位、整理今日工作、搜索过去记录、维护简单待办时，使用此技能。

适合的问题：

- “把这件事记到笔记里”
- “记录一下今天完成了什么”
- “帮我搜索笔记里有没有 PostgreSQL”
- “生成今天的日报”
- “列出我的笔记文件”

## 两阶段调用方式

第一次使用时先调用：

```json
{
  "mode": "help"
}
```

确认适用后再调用：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py list"
}
```

## 常用命令

追加一条笔记到默认当天文件：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py add --text \"完成了 Memory V2 接入检查\""
}
```

追加到指定文件：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py add --file project.md --text \"下一步：检查前端提醒流\""
}
```

添加待办：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py todo --text \"测试每天 9 点提醒\""
}
```

搜索笔记：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py search --query \"Memory V2\""
}
```

列出笔记：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py list"
}
```

生成当日日报：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/note_keeper.py daily"
}
```

## 文件位置

笔记默认保存在：

```text
notes/
```

默认当天笔记文件格式：

```text
notes/YYYY-MM-DD.md
```

## 安全边界

此技能只读写 office 工位内的 `notes/` 目录，不会访问外部路径，不会删除文件。
