# Domain Docs

本仓库是 single-context 项目。工程类 skills 在分析、诊断、重构或写测试前，应先读取项目领域文档，避免偏离 HFD 的暗流主线设计。

## 探索代码前先读

- 根目录 `CONTEXT.md`：项目领域词汇，例如 Core Darkflow v2、Trade Candidate、Candidate Lifecycle、Frozen Entry Plan。
- `docs/adr/`：架构决策记录，尤其是和当前修改区域相关的 ADR。

如果某个文件不存在，不要中断任务；继续用现有上下文工作。

## 文档布局

```text
/
├── CONTEXT.md
├── docs/adr/
└── app/
```

## 使用领域词汇

输出 issue、重构建议、诊断假设、测试名称时，优先使用 `CONTEXT.md` 中定义的词汇。不要把 **Core Darkflow v2**、**Legacy/Control Research**、**Trade Candidate** 等概念混用。

## 标出 ADR 冲突

如果建议和已有 ADR 冲突，需要明确说明，例如：

> 这与 ADR-0001 冲突，但值得重新讨论，因为……
