# LiteLLM Gateway

[![CI](https://github.com/xiongweilin/litellm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/xiongweilin/litellm-gateway/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
A project-specific LiteLLM model gateway for Codex-compatible routing.

This repository contains the gateway configuration, startup scripts, and supporting tools used to route local model requests.

## Responsibility boundary

This repository owns model-endpoint routing and gateway deployment configuration only.

```text
model route / gateway success
!= Work or Run ownership
!= action authorization
!= effect execution authority
!= objective verification or completion
```

Agent/runtime lifecycle and authority remain with the consuming runtime or deployment profile, currently `portable-runtime` / `control-plane` where applicable. This gateway must not become a second owner of provider-neutral runtime semantics.
