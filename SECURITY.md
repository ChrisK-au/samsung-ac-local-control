# Security Policy

## Reporting a Vulnerability

Please report security issues using GitHub's private vulnerability reporting feature.

Do not open public issues for secrets, authentication tokens, or vulnerabilities that could expose local network control.

This project is intended for trusted local networks only. Do not expose the web interface directly to the internet.

Keep `config.yaml` private because it may contain your AC pairing token and local network details.

Treat local runtime files and service logs as private as well. `usage_log.csv`, `schedules.yaml`, and systemd journal output can reveal occupancy patterns, device IPs, and local control history.
