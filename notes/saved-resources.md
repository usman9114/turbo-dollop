# Saved resources

Collected 2026-11-05 from Instagram reels and research chats. Star counts are approximate and will drift.
**Always check the owner name before installing: lookalike/copycat repos exist for popular projects. Read any skill's `SKILL.md` and scripts before installing it.**

## Jobs
| What | Link | Notes |
|---|---|---|
| FreeHire | https://github.com/strelov1/freehire | Open-source IT job search, ~4.7M live postings crawled straight from company ATS pages. MIT. ~3.4k★. Site: https://freehire.me. Free tier; Pro is $5/mo. |
| FreeHire MCP | https://github.com/strelov1/freehire-mcp | Lets Claude search jobs through FreeHire. |
| FreeHire CLI | https://github.com/strelov1/freehire-cli | CLI over the FreeHire API for agents. |
| Career Ops | https://github.com/career-ops-hq/career-ops | AI job-search agent for Claude Code/Codex: scan job boards, score each job 1-5 against your CV, tailor an ATS-friendly CV + cover letter, track applications. MIT, ~80k★, created Apr 2026, updated daily. **Never auto-submits** (README: you press Submit). Free; runs on your existing AI CLI, local models possible. Spinoffs exist (e.g. `andrew-shwetzer/career-ops-plugin-...`, `career-ops-china`, `career-ops-ui`) and are NOT the main project. Check it doesn't fabricate CV facts. |
| Upwork connector | Claude → Customize → Connectors → Upwork | Official connector: find jobs, draft proposals (human approves each submit). Connects cost ~$0.15 each. Don't auto-send generic proposals. |

## Web scraping / letting an LLM read any site
| Repo | Use it for |
|---|---|
| https://github.com/unclecode/crawl4ai | Best free default: page → clean Markdown, optional LLM extraction. |
| https://github.com/firecrawl/firecrawl | Hosted API option, very popular. AGPL licence. |
| https://github.com/ScrapeGraphAI/Scrapegraph-ai | Describe the data you want in plain English. |
| https://github.com/browser-use/browser-use | AI agent that clicks/logs in/navigates. |
| https://github.com/D4Vinci/Scrapling | Anti-bot handling, adaptive selectors, MCP + agent skill. |
| https://github.com/browserbase/stagehand | JS/TS SDK for extraction and interaction. |
| https://github.com/microsoft/playwright-mcp | Claude controls a real browser. |
| https://github.com/jina-ai/reader | Zero-setup: prefix any URL with `https://r.jina.ai/`. |
| https://github.com/Panniantong/Agent-Reach | Agent CLI for many sites. Most social sites need login, Twitter/Instagram/Facebook are "experimental"; use a throwaway account. |
| https://github.com/gosom/google-maps-scraper | Google Maps → lead list (MIT, Docker, has an agent skill). Scraping Maps breaks Google's terms. |

## Claude skills (scraping, reverse engineering, traffic)
| Repo | Notes |
|---|---|
| https://github.com/brightdata/skills | Official Bright Data skills (paid service behind them). |
| https://github.com/firecrawl/skills | Official Firecrawl skills. |
| https://github.com/Cedriccmh/claude-code-skill-scrapling | Scrapling skill. |
| https://github.com/yfe404/web-scraper | Auto strategy selection + Apify Actor generation. |
| https://github.com/SimoneAvogadro/android-reverse-engineering-skill | Decompile Android apps, find API calls. |
| https://github.com/zhaoxuya520/reverse-skill | 44k★ but only created May 2026: verify before trusting. |
| https://github.com/Masriyan/Claude-Code-CyberSecurity-Skill | 22 security skills. |
| https://github.com/zhuyansen/awesome-ai-reverse-engineering | Curated list: Ghidra/IDA MCP, jadx, Frida. |
| https://github.com/khuynh22/mcp-wireshark | Wireshark/tshark MCP server for pcap analysis. |
| (idea) network-recon skill | HAR capture → find hidden JSON API → replay. Not found anywhere; could build. |

Use reverse engineering / traffic capture only on systems you own or are authorized to test.

## Extending what an LLM can do
- Tools/MCP: https://github.com/modelcontextprotocol/servers
- Code sandbox: https://github.com/e2b-dev/E2B
- Agents: https://github.com/langchain-ai/langgraph, https://github.com/crewAIInc/crewAI
- Memory: https://github.com/mem0ai/mem0, https://github.com/letta-ai/letta
- Docs/RAG: https://github.com/run-llama/llama_index
- Structured output: https://github.com/567-labs/instructor, https://github.com/dottxt-ai/outlines
- Run models locally: https://github.com/ollama/ollama, https://github.com/ggml-org/llama.cpp, https://github.com/vllm-project/vllm, https://github.com/open-webui/open-webui

## Sales / outreach
- Harvey: https://github.com/ethanplusai/harvey (MIT, ~4.4k★). Autonomous sales agent on a Claude Code subscription. Needs Instantly (paid) for sending. **Copycats exist (e.g. kush-coder/autopilot-harvey): use only the `ethanplusai` one.**
- Cold-email stack from a reel (~$50/mo): Apify Apollo scrapers + NeverBounce/MillionVerifier + Zapmail mailboxes + a domain + Instantly. Warm up mailboxes 2-4 weeks. UK sole traders can't be cold-emailed without consent; Germany bans unsolicited B2B email.

## Business idea: AI call-answering for tradespeople
- Sell an AI receptionist (missed-call capture, SMS summary, booking) to plumbers/electricians/painters, ~$200-400/mo.
- Reuse a platform: Retell AI (easiest), Vapi (more technical, cheaper headline rate). Verify current pricing.
- Leads: Google Maps scraper → filter 20-100 reviews, no chat/booking → personalised outreach. Demo phone number is the best pitch.
- Get first clients + proof via Upwork voice-agent setup jobs, then sell the retainer directly.
- Open question: which country/city? (Decides platform, phone setup and outreach law.)
- Urdu/Pakistan angle: Uplift AI (YC S25, Urdu voice models) as a voice supplier; COD order-confirmation calls as a first use case. Couldn't find "Rabtah AI".
- Dessa (Block/Jack Dorsey voice model, Oct 2026) is a demo/waitlist only: can't build on it yet.

## Reels checked
| Reel | Creator | Verdict |
|---|---|---|
| Dd_4ACbivLK | @itstundeanthony | Meta's Muse agent reads your saved Instagram posts. Real. |
| DfFYWm7K2bl | @buildwithai.club | Harvey sales agent. Real, "free" needs paid Instantly. |
| DcjDj6fyoZ0 | @jackroberts___ | Scrapling. Real; "700x faster" is a parser benchmark vs BeautifulSoup. |
| DdvaU7uKp6- | @nicksaraevonetakes | $50/mo cold-email stack. Sound advice. |
| DfF4Wd-FWd0 | @valeridoesai | Google Maps → leads (gosom). Real. |
| De9_Xn0DYOj | @kevbuildsapps | Dessa voice model. Real but demo only; "killed cold callers" is hype. |
| Dca0buPTihg | @shashwat___agarwal | Agent Reach. Real; "scrapes everything free" is overblown. |
| DeqmMGqj6BM | @ikramrana.ai | FreeHire. Real. |
| DfBOiXfqycl | @abhijayarora_ | Career Ops job-hunting agent. Real; "740 listings → 68 applied → 12 interviews → 1 offer" is the creator's own claim (repeated in the README). Reel says 76k★, now ~80k. |
| DdIm3-lII8B, DcljTqnsCn1, DePPO_4PpRk | various | Upwork connector in Claude. Real; ignore "it auto-submits". |
| Dda6O2GCz1s | @nicksaraevonetakes | Upwork search method: OR-keyword saved searches. Good. |
| DEFzsYQuyXP | @remoteoliver | Proposals: open with the client's problem. Fine. |
| DdLrNcYtiWy | @ahmadzabir_official | Upwork ranking claims unverified; sells a book. |

Transcripts for these live in `out/transcripts/<id>/` (gitignored, so only in the session that made them).

## To look into later
- Build the network-recon skill (HAR → hidden API).
- Prototype the plumber/electrician AI receptionist template on Retell.
- Try the Upwork connector for voice-agent jobs.
- Fireclaw: unclear which one was meant (AFK-surf/fireclaw, bchewy/fireclaw, raiph-ai/fireclaw). Needs a reel link or clarification.
