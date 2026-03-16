# Contributing to AI Nexus

Thank you for your interest in contributing!

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USER/ai-nexus.git`
3. Create a branch: `git checkout -b feature/your-feature`
4. Make changes, commit, and push
5. Open a Pull Request

## Development Setup

```bash
cp .env.example .env
# Fill in minimal dev values

# Start only Docker services
docker compose up -d neo4j zep-postgres

# Run gateway in dev mode
cd services/gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 5050
```

## Pull Request Guidelines

- Keep PRs focused on a single feature or fix
- Include tests where applicable
- Never commit `.env` files or credentials
- Run `gitleaks detect --source .` before submitting
- Update `SBOM.json` if you add new dependencies

## Security

Do not open public issues for security vulnerabilities — see [SECURITY.md](SECURITY.md).
