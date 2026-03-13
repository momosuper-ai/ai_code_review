from src.core.prompt_builder import build_review_prompt, detect_language


def test_detect_language():
    assert detect_language("Main.java") == "java"
    assert detect_language("app.py") == "python"
    assert detect_language("main.go") == "go"
    assert detect_language("index.ts") == "typescript"
    assert detect_language("App.vue") == "vue"
    assert detect_language("unknown.xyz") == "unknown"


def test_build_review_prompt_contains_key_elements():
    prompt = build_review_prompt("service.py", "+ def hello():\n+     return 'world'")
    assert "service.py" in prompt
    assert "python" in prompt
    assert "安全性" in prompt
    assert "中文" in prompt
    assert "JSON" in prompt


def test_build_review_prompt_with_custom_rules():
    rules = "禁止使用 eval() 函数"
    prompt = build_review_prompt("main.py", "+ eval(input())", custom_rules=rules)
    assert "禁止使用 eval() 函数" in prompt
