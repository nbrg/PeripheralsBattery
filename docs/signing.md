# Code signing

Unsigned Windows programs trigger SmartScreen ("Windows protected your PC") and
some antivirus heuristics. The release pipeline signs both the app and the
installer as soon as a certificate is available - no code changes needed.

## Turning it on

Add two repository secrets (*Settings > Secrets and variables > Actions*):

| Secret | Value |
|---|---|
| `SIGN_PFX_BASE64` | the code-signing certificate (.pfx), base64 encoded: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))` |
| `SIGN_PFX_PASSWORD` | the .pfx password |

`installer/sign.ps1` then signs with SHA-256 and an RFC 3161 timestamp, so
signatures stay valid after the certificate expires. Without the secrets the
step logs "leaving … unsigned" and the release is built anyway.

## Getting a certificate

| Option | Cost | Notes |
|---|---|---|
| [SignPath Foundation](https://signpath.org/) | free for open source | signing happens on their service; the workflow step is swapped for their GitHub action |
| [Certum Open Source Code Signing](https://shop.certum.eu/open-source-code-signing.html) | low yearly fee | issued to an individual; the key lives on a smart card / cloud HSM, so signing runs on your machine or through their cloud signing tool |
| [Azure Trusted Signing](https://learn.microsoft.com/azure/trusted-signing/) | monthly fee | Microsoft-managed; needs identity validation; has an official GitHub action |
| Standard OV certificate (DigiCert, Sectigo, …) | yearly fee | since 2023 keys must live on hardware, so .pfx files are rarely issued any more — prefer a cloud signing option |

A self-signed certificate does **not** help: SmartScreen only trusts
certificates from public CAs, and reputation builds up per certificate as
people download the signed builds.
