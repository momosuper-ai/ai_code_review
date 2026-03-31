FROM python:3.12-slim

WORKDIR /app

# 配置 pip 镜像源
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.trusted-host mirrors.aliyun.com

# 切换 apt 为阿里云镜像源（兼容新旧两种 sources 文件格式）
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|https\?://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
        sed -i 's|https\?://security.debian.org|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i 's|https\?://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list; \
        sed -i 's|https\?://security.debian.org|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list; \
    fi

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（先只安装依赖，利用 Docker 层缓存）
COPY pyproject.toml ./
RUN pip install --no-cache-dir $(python -c "\
import tomllib; \
deps = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; \
print(' '.join(deps))")

# 复制源代码
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# 安装项目本身（不重复安装依赖）
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
