---
name: gmgn-config
description: GMGN API Key and key pair setup. Use this skill when GMGN_API_KEY is not configured, or when the user explicitly runs gmgn-cli config. Handles key pair generation, API Key configuration, and verification.
argument-hint: ""
metadata:
  cliHelp: "gmgn-cli config --help"
---

**All output to the user must be in the same language as the current conversation.**

## When to use this skill

- Triggered automatically by other GMGN skills when `GMGN_API_KEY` is not found in `<cwd>/.env` or `~/.config/gmgn/.env`
- Also applies when the user explicitly runs `gmgn-cli config`

## Setup flow

### Step 1 — Generate key pair and show the link

Run:

```bash
gmgn-cli config
```

The command outputs a pre-filled link. Show the link to the user and ask them to click it to create their GMGN API Key. Tell the user to send back the API Key once it is created.

### Step 2 — Write credentials to .env

Once the user sends back the API Key, run the following command to write `GMGN_API_KEY` and `GMGN_PRIVATE_KEY` together into `~/.config/gmgn/.env`:

```bash
node -e "const fs=require('fs'),os=require('os');const envPath=os.homedir()+'/.config/gmgn/.env';const pemPath=os.homedir()+'/.config/gmgn/keypair.pem';const pem=fs.readFileSync(pemPath,'utf8');const m=pem.match(/(-----BEGIN PRIVATE KEY-----[\s\S]+?-----END PRIVATE KEY-----)/);const pk=m?m[1].replace(/\r?\n/g,'\\\\n'):'';let env=fs.existsSync(envPath)?fs.readFileSync(envPath,'utf8'):'';env=env.split('\n').filter(function(l){return!/^GMGN_API_KEY=|^GMGN_PRIVATE_KEY=/.test(l)}).join('\n').trim();const content=(env?env+'\n':'')+'GMGN_API_KEY=<key_from_user>\n'+'GMGN_PRIVATE_KEY='+pk+'\n';fs.writeFileSync(envPath,content,{mode:0o600});"
```

### Step 3 — Verify configuration

Run the following command to verify that the API Key and local key pair match:

```bash
gmgn-cli track follow-wallet --chain sol
```

- **Success** — Tell the user (in the conversation language):
  - 中文：配置验证成功，可以开始使用了。
  - 繁體：配置驗證成功，可以開始使用了。
  - English: Configuration verified successfully. You are ready to use GMGN.

- **Failure** — Tell the user (in the conversation language):
  - 中文：配置验证失败：API Key 与本地密钥不匹配。\n请确认：1. API Key 是否填写正确；2. 创建 API Key 时，是否使用的是页面自动填入的公钥。
  - 繁體：配置驗證失敗：API Key 與本地密鑰不匹配。\n請確認：1. API Key 是否填寫正確；2. 創建 API Key 時，是否使用的是頁面自動填入的公鑰。
  - English: Configuration verification failed: API Key does not match the local key.\nPlease confirm: 1. Whether the API Key was entered correctly; 2. Whether the public key auto-filled on the page was used when creating the API Key.

### Step 4 — Continue

After successful verification, proceed with the user's original request.
