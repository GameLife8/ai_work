"""Skill 插件包。

每个 skill 是一个子模块（建议是子包），暴露：
    - MANIFEST: dict
    - run(ctx, **params) -> dict

启动时由 ops_platform.loader.load_skills_from_package('skills', registry) 扫描注册。
"""
