# Public Provider profiles

These are conservative bootstrap identities, not exhaustive domain lists.
Never expand them from memory. Use local evidence and current official product
documentation.

| Provider ID | Product | Public bootstrap root | Default |
| --- | --- | --- | --- |
| `openai` | OpenAI, ChatGPT, Codex | `openai.com`, `chatgpt.com` plus reviewed OpenAI static/content roots | Enabled |
| `workbuddy-cn` | WorkBuddy (China) | `workbuddy.cn` | Disabled |
| `kimi` | Kimi | `kimi.com` | Disabled |
| `minimax` | MiniMax | `minimaxi.com` | Disabled |
| `mavis` | Mavis | `mavislabs.ai` | Disabled |

Public identity references: [OpenAI](https://openai.com/),
[ChatGPT](https://chatgpt.com/),
[WorkBuddy China](https://www.workbuddy.cn/work/),
[Kimi](https://www.kimi.com/help/getting-started/overview),
[MiniMax](https://chat.minimaxi.com/download), and
[Mavis](https://mavislabs.ai/).

OpenAI has semantic API/auth probes plus ChatGPT web and transport probes.
Other profiles intentionally start with a product-root web probe. Before
relying on them for automatic failover, observe the actual app/browser flow on
that Mac and add only reviewed exact domains and objective critical probes to
the private overlay.

Product names, domains, app processes, and endpoints can change. Live local
state and current official documentation outrank this reference. If identity
is ambiguous, stop instead of routing a similarly named product.
