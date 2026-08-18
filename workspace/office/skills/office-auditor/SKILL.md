name: office_auditor
description: 扫描 office 工位文件，生成文件结构、类型统计、行数统计和 TODO/FIXME/NOTE 摘要，适合在处理文件任务前快速了解当前工位状态。

# Office Auditor

## 用途

当用户想了解 office 工位里有什么文件、哪些文件可能需要处理、项目里有没有 TODO/FIXME/NOTE 标记，或需要一个轻量级工作区概览时，使用此技能。

适合的问题：

- “帮我检查一下 office 工位里有什么”
- “扫描一下当前工位，看看有没有 TODO”
- “给我一个 office 文件报告”
- “先了解一下这个工位的文件结构”

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
  "command": "python {baseDir}/scripts/audit_office.py"
}
```

## 可选参数

扫描指定子目录：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/audit_office.py --root images_in"
}
```

限制输出的 TODO/FIXME/NOTE 数量：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/audit_office.py --max-notes 20"
}
```

输出 JSON，便于后续自动处理：

```json
{
  "mode": "run",
  "command": "python {baseDir}/scripts/audit_office.py --json"
}
```

## 输出内容

默认输出 Markdown 报告，包括：

- 总文件数、目录数、总字节数
- 文件扩展名统计
- 文本文件行数统计
- 最大文件列表
- TODO/FIXME/NOTE 摘要

## 安全边界

此技能只读取当前 office 工位及其子目录，不会写入、删除、移动文件，也不会访问 office 工位外部路径。
