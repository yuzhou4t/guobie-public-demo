"""Name saved workflow results from their confirmed content, including legacy snapshots."""


def workflow_result_title(artifact, config=None):
    if artifact.get("title"):
        return artifact["title"]
    if artifact.get("type") == "field_research_workflow":
        excerpts = artifact.get("excerpts", [])
        themes = []
        for excerpt in excerpts:
            selected = excerpt.get("annotation", {}).get("themes", [])
            for theme in selected[:3] if len(excerpts) == 1 else selected[:1]:
                if theme.strip() and theme.strip() not in themes:
                    themes.append(theme.strip())
        subject = "、".join(themes[:3]) or "访谈材料"
        kinds = {e.get("kind") for e in excerpts}
        kind = "访谈摘录与观点卡" if len(kinds) > 1 else "观点卡" if "viewpoint" in kinds else "访谈摘录"
    else:
        subject = (config or {}).get("topic_name") or next(
            (
                o["snapshot"].get("title")
                for o in artifact.get("observations", [])
                if o["snapshot"].get("title")
            ),
            "政策动态",
        )
        kind = "专题追踪结果"
    return f"{subject[:64]}—{kind}"
