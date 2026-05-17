# Issue Tracker：GitHub

本仓库的 Issue 和 PRD 使用 GitHub Issues 管理。相关 skill 需要创建、读取、评论或更新 issue 时，优先使用 `gh` CLI。

## 仓库

`Esingpawn/hfd-trading-research`

## 操作约定

- **创建 issue**：`gh issue create --title "..." --body "..."`
- **读取 issue**：`gh issue view <number> --comments`
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments`
- **评论 issue**：`gh issue comment <number> --body "..."`
- **添加/移除标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭 issue**：`gh issue close <number> --comment "..."`

在仓库目录内执行时，`gh` 通常可以从 `git remote -v` 自动识别仓库。

## 当 skill 说“发布到 issue tracker”

创建一个 GitHub Issue。

## 当 skill 说“读取相关 ticket”

执行 `gh issue view <number> --comments`。
