"""Deterministic fixtures for local demos. They are explicitly labelled, not LLM results."""


def mock_output(schema: str, context: dict):
    if schema == "TopicBatch":
        names = [
            "用 Python 做第一个 AI 应用",
            "AI 训练师每天在做什么",
            "AI 应用为什么需要人工审核",
            "如何给 AI 回答做一个评测表",
            "提示词不是越长越好",
            "如何把课程笔记整理成知识卡片",
            "用一个清单检查AI输出是否完整",
            "为什么需要保存每次人工修改",
            "AI编程项目怎样拆成小步骤",
            "从标注到评测：训练师入门练习",
        ]
        offset = (context.get("topic_round", 0) * 5) % len(names)
        names = names[offset:] + names[:offset]
        return {
            "topics": [
                {
                    "id": f"topic-{i + 1}",
                    "title": title,
                    "angle": "从一个小实验理解工作流、状态与人工确认",
                    "audience_value": "知道第一步该做什么，以及怎样验证输出",
                    "evidence": "演示数据：按课程与受众策划，无实时热度数据",
                }
                for i, title in enumerate(names[: context["task_input"]["topic_count"]])
            ]
        }
    if schema == "Article":
        title = context["selected_topic"]["title"]
        feedback = context.get("human_feedback", "")
        intro = f"本轮修改要求：{feedback}\n\n" if feedback else ""
        body = (
            intro
            + """## 先把目标缩小到一个问题

学习 AI 应用开发时，最容易遇到的问题不是不会写复杂代码，而是不知道怎样判断一次输出是否可用。先做一个小实验：输入一段课程笔记，让程序整理出三个知识点，再由自己检查。这比一开始搭建很多模块更容易发现问题。

本文是本地演示模式的示例内容，用来测试生成、审核、修改和导出流程，不代表真实模型的输出质量，也不代表任何平台的热门趋势。

## 一个应用包含哪些部分

可以把流程理解为一条作业流水线。输入是原始笔记，模型调用负责整理，结构校验负责检查结果是否缺字段，人工审核负责判断是否准确。数据库保存进度，这样程序关闭后，我们仍能知道上一轮做到了哪一步。

模型能够生成看起来流畅的文字，但流畅不等于正确。涉及课程内容、接口用法和操作步骤时，需要对照原始资料。如果资料里没有某个结论，就把它写成待核实项，不要补出一个像真的答案。

## 用一个小步骤开始

第一步，准备一段不含个人信息的学习笔记。第二步，写清楚输出要求，例如只整理三个知识点，每个知识点给出一个练习建议。第三步，检查是否遗漏前提条件。第四步，记录你修改了什么，再尝试第二次生成。

下面的代码只演示普通 Python 数据检查，不调用任何外部模型，也不依赖特定模型 SDK：

```python
points = ["定义输入", "检查输出", "记录反馈"]
for index, point in enumerate(points, 1):
    print(index, point)
```

运行后会依次打印三个步骤。这个例子很小，但提醒我们：先让输入、输出和验证标准明确，再逐步加入模型 API、数据库和页面。调试时一次只改变一个条件，才能看清问题来自哪里。

## 人工审核为什么不能省

假设模型把某个工具的旧接口写成新接口，排版再漂亮也无法让代码运行。审核者应检查概念、代码、来源和措辞，并把意见写得具体。例如“解释状态是什么，再给一个生活中的类比”，通常比“重新写好一点”更容易执行。

人工修改后也应保留原稿和修改记录。这样我们可以比较哪个提示条件有效，哪些错误反复出现。AI 训练师相关工作还可能涉及数据整理、标注与评测，具体岗位要求应以真实用人需求和课程材料为准，不能把这些工作都说成训练基础大模型。

## 今天可以完成的练习

选择你熟悉的知识点，写下三个检查问题：读者能否看懂，步骤能否执行，结论是否有依据。先手工做一遍，再让 AI 辅助整理。最后记录耗时和错误类型。这个小样本只能帮助改进自己的流程，不能据此推断所有用户都喜欢什么。

下一次再加入模型调用，并保留人工确认环节。先把一个小任务做完整，再扩大到更多内容类型。
"""
        )
        return {
            "title": title,
            "digest": "从一个小实验理解 AI 应用开发的输入、校验与人工审核。",
            "markdown": body,
            "fact_checks": [
                "这是演示样稿，真实发布前应重新生成并核查",
                "课程与岗位描述应对照公司真实资料",
            ],
        }
    if schema == "CardPack":
        slides = [
            {
                "title": "第一个 AI 应用，从小任务开始",
                "subtitle": "AI 编程入门 · 本地演示样稿",
                "bullets": [
                    "先输入一段笔记，让程序整理三个知识点",
                    "把输入、输出和检查标准写清楚",
                    "先完成小实验，再逐步增加功能",
                ],
                "takeaway": "先明确目标，再调用模型",
            },
            {
                "title": "把应用拆成四个步骤",
                "subtitle": "一条可检查的流水线",
                "bullets": [
                    "输入：准备不含个人信息的课程笔记",
                    "生成：指定知识点数量与输出格式",
                    "校验：检查字段、概念与操作步骤",
                    "审核：人工确认之后再继续加工",
                ],
                "takeaway": "流畅的文字，也需要核对",
            },
            {
                "title": "先运行一个小例子",
                "subtitle": "普通 Python，不调用模型 API",
                "bullets": ["只改变一个条件，便于定位问题", "先明确验证标准，再逐步接入模型"],
                "code": 'points = ["定义输入", "检查输出"]\nfor i, point in enumerate(points, 1):\n    print(i, point)',
                "takeaway": "让每一步都有可检查的结果",
            },
            {
                "title": "给输出加一道人工审核",
                "subtitle": "事实、代码、来源、措辞",
                "bullets": [
                    "发现问题时给出具体修改意见",
                    "保留原稿、定稿与修改记录",
                    "技术接口与课程描述要对照真实资料",
                ],
                "takeaway": "把修改意见变成下一轮输入",
            },
            {
                "title": "今天就能做的小练习",
                "subtitle": "选一个熟悉的知识点",
                "bullets": ["读者是否看得懂？", "步骤是否能执行？", "结论是否有依据？"],
                "takeaway": "记录错误类型，逐步改进流程",
            },
        ]
        count = context["task_input"]["card_count"]
        selected = [slides[0], *slides[1 : count - 1], slides[-1]]
        return {
            "title": "第一个AI应用怎么做",
            "caption": "【演示样稿】从一个小任务理解输入、校验与人工审核。正式发布前请完成事实核查。",
            "tags": ["AI编程", "Python入门", "AI应用开发"],
            "slides": selected,
        }
    raise ValueError(f"Unknown fixture schema: {schema}")
