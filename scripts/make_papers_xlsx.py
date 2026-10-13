"""Build papers_limitations.xlsx from the prior-work research (see reports/)."""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

OUT = Path(__file__).resolve().parent.parent / "papers_limitations.xlsx"

HEADERS = [
    "#",
    "Paper / project name",
    "Venue / year",
    "Link",
    "Category",
    "Verification",
    "Limitations relevant to our project",
]

# Verification: Full = read in full; Abstract = abstract/page only; Snippet = search snippet or aggregator only
ROWS = [
    ("PARALLAX: Defense of the Clones (Tsouvalas, Nikiforakis)", "IEEE eCrime 2025",
     "https://securitee.org/files/clones_ecrime2025.pdf", "Environment cloning", "Full",
     "Not read-only: installs OpenResty/Redis and rewrites the production web-server config. LAMP/Apache and DB columns only, no multi-service Docker estates. "
     "Sends raw sensitive columns to the local LLM instead of sanitizing first; detector coverage is low (heuristics 24.8%, role-based 38.1%, LLM 2.3%). "
     "LLM fills data values only, not environment structure. No versioned/immutable artifact; reset is a 10-minute window per IP, not a per-session twin. No shell/command execution layer."),
    ("TwinPot-Gen (Yigit, Huseynov, Canberk)", "IEEE ICC 2026",
     "https://research.itu.edu.tr/en/publications/twinpot-gen-a-generative-semantic-digital-twin-honeypot-framework/", "Digital-twin honeypot", "Abstract",
     "'Twin' is a 6G network model, not a container-level replica of a server. Sanitization mechanism and data flow unknown (abstract only). No evidence of real command execution, versioning or per-session isolation. "
     "Must be read in full before claiming novelty on sanitization + LLM + twin."),
    ("TwinPot (smart seaports)", "ICC 2023",
     "https://arxiv.org/pdf/2310.12880", "Digital-twin honeypot", "Abstract",
     "Simulation-fed twin of a seaport/ICS setting; no mirroring of a real Docker server, no LLM, no sanitization of real data, no attacker shell."),
    ("Cyber-Twin Based Honeypot (SUTD)", "arXiv 2509.09222, 2025",
     "https://arxiv.org/html/2509.09222v1", "Digital-twin honeypot", "Full",
     "Replica of a water-treatment plant (OT). 9-day single qualitative case; no sanitization step, no fidelity metrics, no Docker discovery, no LLM, no shell-level interaction."),
    ("DiTwinIHon", "IEEE CSR 2023",
     "https://zenodo.org/records/10227428", "Digital-twin honeypot", "Abstract",
     "Conceptual framework reportedly not implemented; ICS focus; no evaluation to compare against."),
    ("HoneyPLC (Lopez Morales et al.)", "ACM CCS 2020",
     "https://adamdoupe.com/publications/honeyplc-ccs2020.pdf", "Environment cloning", "Abstract",
     "Profiles a PLC via Nmap/S7comm/SNMP and replays it; protocol emulation only. Later work (ICSLure, arXiv 2509.04080) shows it is detectable. No filesystem/OS, no sanitization of secrets, no LLM."),
    ("Fraunholz et al.: scan-to-Honeyd configuration (DFKI)", "arXiv 2111.03884",
     "https://arxiv.org/pdf/2111.03884", "Environment cloning", "Full",
     "Clones network fingerprints only (Nmap -> k-means -> Honeyd). No filesystem, services, users or app data; no secret handling; no execution layer. Venue/year not confirmed."),
    ("Chat2Pot (DFKI)", "INFOCOM 2026 (demo/poster track, verify)",
     "https://www-live.dfki.de/fileadmin/user_upload/import/17289_Chat2Pot_-_Chatting_a_Honeypot_into_Existence.pdf", "LLM environment generation", "Full",
     "Generates Dockerfiles from a text description, not from a real system, so no ground-truth consistency. 66.67% build success over 30 generations. Synthetic data left as future work; no sanitization, no runtime validation."),
    ("Benedetti MSc thesis: LLM infrastructure-as-code honeypots (Univ. Bologna)", "Thesis, 2026",
     "https://amslaurea.unibo.it/id/eprint/39087/", "LLM environment generation", "Snippet",
     "Not peer reviewed; PDF could not be parsed. Generates from a threat model (Meta Attack Language), not from observation of a real server. Reported 100% deployment success is unverified."),
    ("Koney (Kubernetes deception operator)", "arXiv 2504.02431, 2025",
     "https://arxiv.org/abs/2504.02431", "Container deception", "Abstract",
     "Plants honeytokens/decoys in real running workloads, so it does not isolate attacker execution from production. Not a twin and no environment generation."),
    ("HoneyKube", "IEEE SPW 2023",
     "", "Container deception", "Abstract",
     "Purpose-built Kubernetes honeypot, not a replica of a production system. No sanitization, no LLM, no per-session twin."),
    ("shelLM: LLM in the Shell (Sladic et al.)", "EuroS&P Workshops 2024; arXiv 2309.00155",
     "https://arxiv.org/html/2309.00155v3", "LLM shell honeypot", "Abstract",
     "Pure LLM emulation: no real execution, so output can drift and contradict itself. State is prompt history only. Evaluation is a small expert study (TNR 0.90). No isolation, secrets, or environment-from-real-system story."),
    ("VelLMes (Sladic et al.)", "arXiv 2510.06975, 2025",
     "https://arxiv.org/html/2510.06975v1", "LLM shell honeypot", "Abstract",
     "Multi-service (SSH, MySQL, POP3, HTTP) but pure LLM. Consistency by replaying history, not a ground-truth state store. Only ~30% judged it real (vs ~35% for real Ubuntu). No validation against execution evidence."),
    ("HoneyGPT (Wang et al.)", "arXiv 2406.01882, 2024",
     "https://arxiv.org/html/2406.01882v2", "LLM shell honeypot", "Abstract",
     "ChatGPT behind Cowrie's protocol proxy; no real execution. State register lives in the prompt and is LLM-written, so errors compound. Latency and cost per command; no secret or isolation story."),
    ("HoneyLLM", "IEEE CNS 2024 (DOI 10.1109/CNS62487.2024.10735663)",
     "", "LLM shell honeypot", "Abstract",
     "Abstract mentions a 'hybrid architecture' we could not read. Must read in full: if it executes commands for real, our core novelty claim is weakened."),
    ("Otal & Canbaz: LLM Honeypot", "arXiv 2409.08234, 2024",
     "https://arxiv.org/abs/2409.08234", "LLM shell honeypot", "Abstract",
     "Fine-tuned model that emulates SSH output; no real execution, no state guarantees, fine-tuning data tied to a fixed environment. Metrics are text similarity, not attacker behavior."),
    ("LLMHoney", "arXiv 2509.01463, 2025",
     "https://arxiv.org/html/2509.01463v1", "LLM shell honeypot", "Abstract",
     "Dictionary cache + virtual filesystem, LLM for the rest; no real OS. Hallucination 5.8%-25.9% depending on model. Only 138 isolated commands tested; no public repo found."),
    ("SBASH / HoneywareX", "arXiv 2510.21459, 2025",
     "https://arxiv.org/html/2510.21459v1", "LLM shell honeypot", "Abstract",
     "Native handlers + RAG LLM + 'command not found'. Deterministic layer is hand-built emulation, not a real Linux. About 2 s latency; only 9 human testers."),
    ("LLM-SSHH", "MDPI Applied System Innovation 9(5), 2026",
     "https://www.mdpi.com/2571-5577/9/5/101", "LLM shell honeypot", "Snippet",
     "Rejects replies failing a state-snapshot check, but against simulated state, not real execution evidence. Page returned 403; details unverified."),
    ("AdvancedShelLM", "arXiv 2606.27990, 2026",
     "https://www.emergentmind.com/papers/2606.27990", "LLM shell honeypot", "Snippet",
     "Manager LLM approves Worker LLM output (LLM validating LLM) with JSON-delta filesystem. No real execution. Human study n=9 flagged real Ubuntu as a honeypot 70% of the time, so the numbers are hard to interpret. Seen via aggregator only."),
    ("Cowrie LLM backend", "Cowrie docs",
     "https://docs.cowrie.org/en/latest/LLM.html", "LLM shell honeypot", "Abstract",
     "Experimental; keeps only the last 10 commands, 1-3 s per command, downloads simulated. No evaluation published."),
    ("Beelzebub", "GitHub project",
     "https://github.com/mariocandela/beelzebub", "LLM shell honeypot", "Abstract",
     "Optional LLM for SSH responses; no real execution or isolated twin. State handling unclear. Licence reported inconsistently across sources."),
    ("Honeyval", "arXiv 2605.29963, 2026",
     "https://www.emergentmind.com/papers/2605.29963", "LLM service honeypot / evaluation", "Snippet",
     "Evaluates fidelity of LLM HTTP/REST honeypots (pass@1 71.2%-97.5%) from OpenAPI specs and tests; does not check cross-request state. Prompts that raise engagement raise detection or cut fidelity. Read via secondary summary only."),
    ("Reti et al.: Act as a Honeytoken Generator", "arXiv 2404.16118, 2024",
     "https://arxiv.org/abs/2404.16118", "AI decoy generation", "Abstract",
     "Each artifact generated independently; no cross-artifact consistency (user in passwd vs DB vs logs). No sanitization of real data and no integration with a running environment."),
    ("Landsborough, Rowe, Nguyen: Database Deception using LLMs", "Naval Postgraduate School",
     "", "AI decoy generation", "Snippet",
     "Generates fake records for four SQLite DBs; PDF unreadable so schema validation and results are unverified. Standalone database, not a full environment."),
    ("Whitham: honeyfile generation; Ben Salem & Stolfo: decoy documents", "HICSS 2017; DIMVA 2011",
     "", "AI decoy generation", "Abstract",
     "Pre-LLM template/n-gram generation of single files; no environment-wide consistency or service behavior."),
    ("Honeyquest", "RAID 2024; arXiv 2408.10796",
     "https://arxiv.org/abs/2408.10796", "Deception evaluation", "Abstract",
     "Human-subject method for measuring how enticing decoys are; measures decoy appeal, not a generation or execution system. Useful as an evaluation method only."),
    ("Honeyfile Camouflage", "arXiv 2405.04758",
     "https://arxiv.org/abs/2405.04758", "Deception evaluation", "Abstract",
     "Semantic filename-plausibility metric for honeyfiles; covers names only, not contents or cross-file consistency."),
    ("Cowrie proxy mode + Backend Pool", "Cowrie docs",
     "https://docs.cowrie.org/en/latest/BACKEND_POOL.html", "Hybrid / isolation", "Snippet",
     "Closest open-source execution analogue: real QEMU/libvirt VMs, but one VM per source IP, not per session. No LLM, no environment from a real server, no sanitization. VM startup cost; operational details unverified."),
    ("HoneySSH (Lewis)", "GitHub project",
     "https://github.com/josephlewis42/honeyssh", "Hybrid / isolation", "Abstract",
     "Per-session sandbox destroyed on disconnect, but commands are emulated, not real binaries. README only advises gVisor. No LLM, no environment generation."),
    ("HoneyCloud", "2012",
     "https://www.scitepress.org/Papers/2012/41296/41296.pdf", "Hybrid / isolation", "Snippet",
     "Fresh VM per attacker (2012); old threat model, no LLM, no sanitized environment, no analytics beyond logging. Seen as snippet only."),
    ("LLMPot (ICS honeypot)", "arXiv 2405.05999, 2024",
     "https://arxiv.org/abs/2405.05999", "Hybrid / isolation", "Snippet",
     "ICS protocol focus; reportedly runs real services in Docker with an LLM, but this is unverified. Not a general Linux shell, no sanitization pipeline."),
    ("Patents: honeypot asset cloning (US 11290322) and container pools (US 11757936)", "USPTO",
     "https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/11290322", "Patents", "Snippet",
     "Claims not read. May overlap with scan-derived cloning or per-connection container pools; check freedom-to-operate before commercial claims."),
    ("Bitter Harvest: Systematically Fingerprinting Low- and Medium-interaction Honeypots (Vetterl, Clayton)", "USENIX WOOT 2018",
     "https://www.cl.cam.ac.uk/~amv42/papers/vetterl-clayton-bitter-harvest-woot-18-slides.pdf", "Fingerprinting / threat model", "Snippet",
     "Shows Cowrie/Kippo are identifiable from SSH handshake differences. A real-Linux twin does not remove SSH front-end tells, so we need real OpenSSH or a fingerprint-preserving proxy. Numbers from slides/snippets."),
    ("Gotta Catch 'em All (Srinivasa et al.)", "arXiv 2109.10652, 2021",
     "https://arxiv.org/abs/2109.10652", "Fingerprinting / threat model", "Full",
     "Internet-wide detection of 21,855 honeypots; shows scanners find emulated services. It does not test gVisor/microVM-based twins, so we must test our own fingerprint resistance."),
    ("Rouxii (AI pentester vs honeypots)", "arXiv 2609.26555, Sept 2026",
     "https://arxiv.org/abs/2609.26555", "Fingerprinting / threat model", "Full",
     "An LLM attacker with a counter-deception prompt raised honeypot identification from 19% to 97%. Defines the 2026 adversary we must evaluate against; body was not fully readable."),
    ("Ghost Without Shell", "arXiv 2606.28006, 2026",
     "https://arxiv.org/abs/2606.28006", "Attack data", "Full",
     "99.23% of authenticated SSH sessions were non-interactive, so our execution plane must support 'ssh host cmd' exec sessions, not just PTY shells. Dataset is Cowrie-style logs, not real-execution twins."),
    ("SoK: Honeypots & LLMs (Bridges et al.)", "arXiv 2510.25939, rev. Apr 2026",
     "https://arxiv.org/abs/2510.25939", "Survey", "Full",
     "Survey, so it has no system to compare. Detection-vector taxonomy and evaluation vocabulary should anchor our related-work and evaluation sections; details only read from the abstract."),
    ("Automated ATT&CK mapping of Cowrie sessions", "arXiv 2512.05321; Aalborg thesis",
     "https://arxiv.org/pdf/2512.05321", "Attack analytics", "Snippet",
     "Heuristic, command-string-based mapping only. No attack-path graph, no filesystem-diff or process-tree evidence. Our dashboard novelty is modest; do not oversell."),
    ("CyberLab honeynet dataset (Zenodo)", "Dataset, 2019-2020",
     "", "Evaluation dataset", "Snippet",
     "Cowrie-based baseline dataset (~50 nodes); licence unchecked; sessions from low-interaction honeypots, so it is not directly comparable to a real-execution twin."),
    ("Trend Micro: container-escape attempts against Docker", "Threat report, 2021",
     "https://trendmicro.com/en_nl/research/21/b/threat-actors-now-target-docker-via-container-escape-features.html", "Isolation risk", "Abstract",
     "Evidence that attackers try container escape on Docker honeypots. No surveyed paper quantifies escape/egress risk for real-execution twins, so we must test and report it ourselves."),
]


PRODUCT_HEADERS = [
    "#",
    "Product / service",
    "Type",
    "Link",
    "What the site says it does (scraped with Firecrawl, 2026-10-12)",
    "Limitations relevant to our project",
    "Basis",
]

# Basis: "Vendor page" = claim from the product's own site; "Competitor claim" = from a rival's comparison page;
# "Inferred" = our reasoning from what the page does not say.
PRODUCT_ROWS = [
    ("Thinkst Canary", "Commercial deception appliance", "https://canary.tools/",
     "Decoys for Windows file server, router, Linux web server, NAS, SQL server, cloned site, plus tokens (AWS keys, Kubeconfig, Word/Excel/PDF, etc.). "
     "Claims high-interaction, hardware/VM/cloud/container deployment in under 2 minutes, near-zero false positives, no ML or anomaly detection. $7,500 per year.",
     "Closed source and paid. Each decoy is configured by hand; it does not build a twin from a discovered Docker estate or generate synthetic data with AI. "
     "Built to alert on intrusion, not to run a per-session disposable shell or keep evidence separate from AI output. 'Cloned site' is a web page clone only. "
     "Rival Acalvio calls it 'roll-your-own' and hard to scale (competitor claim).",
     "Vendor page + competitor claim"),
    ("CounterCraft", "Commercial deception platform", "https://www.countercraft.eu/",
     "Markets an AI 'Deception Expert' that designs and deploys deception environments from your real architecture, 'digital twins', high-interaction decoys, an 'Engager' that talks to attackers, "
     "agentless and no production systems touched, 0% false positives, deployment in under 30 days.",
     "Closest commercial claim to our idea, so it weakens a product-level novelty claim. But it is closed: how it discovers, sanitizes, isolates and validates is not published, so we cannot compare mechanisms or reproduce it. "
     "Marketing claims (0% false positives, indistinguishable decoys) have no public evaluation. Enterprise pricing; no student-accessible version.",
     "Vendor page"),
    ("Acalvio ShadowPlex", "Commercial deception platform", "https://www.acalvio.com/",
     "AI-driven placement of dynamic decoys, honeytokens, deceptive credentials and 'HoneyPaths'. Agentless, cloud/on-prem/MSP. Its comparison page says it supports low, medium and high-interaction decoys with built-in containment, "
     "ATT&CK mapping and adversary path analysis. 25 patents.",
     "Proprietary, so internals are unknown; patents may overlap with parts of our design (check before commercial claims). Interaction level not stated on the home page. "
     "Aimed at enterprise detection and response, not at studying attacker behavior in a replica of one server. No public evaluation of realism. Pricing not published.",
     "Vendor page"),
    ("Fortinet FortiDeceptor", "Commercial deception appliance", "https://www.fortinet.com/products/fortideceptor",
     "Decoys and lures for IT/OT/IoT, Active Directory deception, ransomware bait files, SaaS/on-prem/cloud, SOAR playbooks to deploy decoys on demand.",
     "Tied to the Fortinet security fabric. Rival Acalvio says it is mostly low-interaction, manually configured and recommends lures based on real accounts (competitor claim, unverified). "
     "Decoys come from a catalog or uploads, not generated from a sanitized profile of the real server. No LLM-based environment or response synthesis.",
     "Vendor page + competitor claim"),
    ("Zscaler Deception", "Commercial deception platform", "https://www.zscaler.com/products-and-solutions/deception-technology",
     "Decoys, lures and breadcrumbs across endpoints, AD, cloud workloads, IoT/OT, inside the Zero Trust Exchange. Includes decoy LLM chatbots and APIs and claims deterministic alerts.",
     "Designed to alert on lateral movement inside a customer network, not to study attackers in a disposable replica. Needs the Zscaler platform and is closed. "
     "Decoys are not generated from a sanitized copy of a specific Docker server. Decoy LLM chatbots are a different goal (detecting attacks on GenAI), not response synthesis for a shell.",
     "Vendor page"),
    ("Proofpoint Shadow, CyberTrap Deceptor, Commvault Threatwise", "Commercial deception platforms", "https://www.techtarget.com/cybersecurity/tip/7-top-deception-technology-vendors-for-active-defense",
     "Per TechTarget: Shadow auto-creates and scales tailored deceptions; CyberTrap adapts decoy layout, services and complexity to attacker behavior; Threatwise's AI recommends decoy placement.",
     "Known only from a third-party summary, not the vendors' own pages. All closed and enterprise priced; no public detail on sanitization, isolation or evidence handling.",
     "Third-party summary"),
    ("T-Pot (Deutsche Telekom Security)", "Open-source honeypot platform", "https://github.com/telekom-security/tpotce",
     "Docker-based platform running 20+ honeypots (Cowrie, Dionaea, Conpot, Beelzebub, Galah and others) with the Elastic Stack, attack maps and tools. LLM honeypots Beelzebub and Galah need Ollama.",
     "Needs 8-16 GB RAM and 128 GB disk, a single NIC, and warns that 'a system compromise can never be ruled out'. It bundles independent honeypots; none is a twin of a real server and there is no sanitization or environment builder. "
     "Honeypots are Docker containers sharing the host kernel (not microVM isolation). Dashboard is generic Kibana, with no graph of a base environment or per-session overlay. Docs say sensitive data must not be added, so it offers no help cloning realistic data safely.",
     "Vendor page (README)"),
    ("Cowrie", "Open-source SSH/Telnet honeypot", "https://github.com/cowrie/cowrie",
     "Medium-interaction emulated UNIX shell in Python with a fake filesystem pickle (a Debian 5.0 image) and canned command outputs. High-interaction proxy mode forwards to another system or a pool of QEMU servers. Experimental LLM mode.",
     "Default filesystem and outputs are static and dated, not generated from the real server, and are a known fingerprinting target (Bitter Harvest). Emulated commands do not match real binaries. "
     "Proxy mode gives real execution but with no LLM gap-filling, no sanitized environment builder and per-IP, not per-session, VMs. LLM mode is marked experimental. SSH/Telnet only.",
     "Vendor page (README)"),
    ("Beelzebub", "Open-source low-code deception runtime", "https://github.com/beelzebub-labs/beelzebub",
     "SSH, HTTP, TCP, Telnet and MCP services defined in YAML, with LLM-powered responses. GPL v3. MCP decoys give evidence of prompt injection but 'do not guarantee detection'.",
     "LLM answers are the main mechanism; the page does not say whether any commands run for real, so it appears to be emulation. No sanitization pipeline, no versioned environment, no per-session isolated twin, "
     "and no validation of LLM output against real evidence. GPL v3 licence affects reuse of its code in our project.",
     "Vendor page (README)"),
    ("Galah", "Open-source LLM web honeypot", "https://github.com/0x4D31/galah",
     "Answers arbitrary HTTP requests with LLM-generated headers and bodies (OpenAI, Google, Anthropic, Cohere, Ollama), caching responses per request and port.",
     "Its own README calls it a 'fun weekend project' and warns it may be identified by network fingerprinting, slow LLM responses and non-standard replies. HTTP only. "
     "Cache-by-request means no real application state across requests. Risk of 'Denial of Wallet' without API limits. No validation against a schema or evidence, no environment generation.",
     "Vendor page (README)"),
    ("OpenCanary (Thinkst)", "Open-source low-interaction honeypot", "https://github.com/thinkst/opencanary",
     "Modular daemon emulating services (SMB, SNMP, git, redis, mongo, HTTP proxy, telnet and others) with JSON config and alerts when touched.",
     "Alerts on touch rather than hosting an interactive shell or replica. Some modules need specific OS support, and the portscan module is disabled when run in Docker, so our container-based deployment would lose features. No AI, no synthetic data.",
     "Vendor page (README)"),
    ("Dionaea", "Open-source malware-capture honeypot", "https://github.com/DinoTools/dionaea",
     "Exposes service emulations (FTP, HTTP, SMB, MySQL, MSSQL, MQTT, SIP, UPnP, TFTP and others) to capture exploits and malware, with embedded Python and shellcode detection.",
     "Focus is capturing malware and exploits, not interactive attacker sessions. The README does not state its interaction level (we infer low). No shell, no environment cloning, no LLM, no analytics beyond logs.",
     "Vendor page + inferred"),
    ("HoneyFactory (Yu, Xin, Zhang)", "Academic container honeynet system", "https://www.mdpi.com/2079-9292/13/2/361",
     "Scans a protected network's hosts, ports and services, then generates a Docker/Calico honeynet that mirrors it, using off-the-shelf honeypots (Glastopf, kippo, Cowrie) and vulnerable apps. Gaussian HMM scores the attack stage. "
     "Simulated 100% of segments, 60/60 hosts, 870/950 services (91.6%); 71.3% overall deception success.",
     "Mirrors network topology and services only; no filesystem, app data or users, and no sanitization of real data. No LLM. Some services cannot be auto-simulated. "
     "Deception success is lower in practice than in theory, and the 'information utilization' module was left unimplemented. Containers share the host kernel; reuse across attackers is not per-session.",
     "Paper (scraped)"),
    ("Containerized cloud-based honeypot deception (Sci. Reports 2023)", "Academic, T-Pot based", "https://www.nature.com/articles/s41598-023-28613-0",
     "Experimental testbed built on Deutsche Telekom's T-Pot: Docker containers plus an ELK stack for administration and analysis.",
     "Inherits T-Pot's limits: independent honeypot containers, no twin of a real server, no sanitization or AI generation, and no per-session disposable environment. Seen via search excerpt only.",
     "Search excerpt"),
]


def style_sheet(ws, widths, link_col):
    header_fill = PatternFill("solid", fgColor="1F3864")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = header_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.alignment = Alignment(vertical="top", wrap_text=True)
        link = r[link_col]
        if link.value:
            link.hyperlink = link.value
            link.font = Font(color="0563C1", underline="single")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions


def main() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Papers and limitations"
    ws.append(HEADERS)
    for i, row in enumerate(ROWS, start=1):
        ws.append([i, *row])
    style_sheet(ws, [5, 44, 26, 48, 24, 13, 110], link_col=3)

    ps = wb.create_sheet("Products and services")
    ps.append(PRODUCT_HEADERS)
    for i, row in enumerate(PRODUCT_ROWS, start=1):
        ps.append([i, *row])
    style_sheet(ps, [5, 32, 28, 48, 80, 90, 22], link_col=3)

    notes = wb.create_sheet("Notes")
    for line in [
        "Source: reports/LLM honeypot digital twin prior work.md and research_notes/ in the same folder.",
        "Verification: Full = read in full; Abstract = abstract or page only; Snippet = search snippet or aggregator summary only (re-check before citing).",
        "Empty Link cells mean no verified URL was recorded.",
        "Re-resolve 2026 arXiv IDs before using them in a bibliography.",
        "'Products and services' sheet: scraped from vendor/GitHub pages with Firecrawl on 2026-10-12. Basis column says whether a point is a vendor claim, a competitor claim, or our inference.",
        "Vendor claims are marketing; nothing in that sheet is independently tested.",
        "Product gap summary: none of the products found build a sanitized, versioned twin from a discovered Docker estate AND run attacker commands in a per-session disposable real Linux with LLM used only for validated gaps.",
        "Closest commercial claim: CounterCraft markets AI-built 'digital twin' deception from your real architecture, but its mechanism is closed and unverifiable.",
    ]:
        notes.append([line])
    notes.column_dimensions["A"].width = 140

    wb.save(OUT)
    print(f"Wrote {OUT}: {len(ROWS)} papers, {len(PRODUCT_ROWS)} products")


if __name__ == "__main__":
    main()
