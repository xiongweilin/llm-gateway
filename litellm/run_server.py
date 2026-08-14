"""LiteLLM 服务入口：等价于 console script `litellm`，但可被 venv python
直接执行（不依赖 uv trampoline，项目目录移动后仍可用）。
"""

import litellm


if __name__ == "__main__":
    litellm.run_server()
