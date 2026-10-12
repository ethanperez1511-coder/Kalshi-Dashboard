# Scripted A1 capacity retry (ruled 2026-10-12)

**Ruling:** a restricted OCI user that can only create instances. Its API key
lives only on your Mac, never in GitHub.

## 1. A compartment that bounds the blast radius
☰ → **Identity & Security** → **Compartments** → **Create compartment**
`kalshi`. Create the VCN and the VM in it (SPEC §2.2, choosing compartment
`kalshi` in the dropdowns). The retry user's rights stop at this compartment.

## 2. Group, user, policy
1. ☰ → Identity & Security → **Domains** → Default → **Groups** → Create
   `kalshi-launchers`.
2. **Users** → Create `kalshi-retry` (any email you control), then add it to
   `kalshi-launchers`. No console password is needed; it only uses an API key.
3. ☰ → Identity & Security → **Policies** (root compartment) → Create
   `kalshi-launch-only`:

```
Allow group kalshi-launchers to manage instances in compartment kalshi where request.permission = 'INSTANCE_CREATE'
Allow group kalshi-launchers to inspect instances in compartment kalshi
Allow group kalshi-launchers to read instance-images in compartment kalshi
Allow group kalshi-launchers to read app-catalog-listing in tenancy
Allow group kalshi-launchers to use virtual-network-family in compartment kalshi
Allow group kalshi-launchers to manage volume-family in compartment kalshi where request.permission = 'VOLUME_CREATE'
Allow group kalshi-launchers to manage compute-capacity-reports in tenancy
```

**Where this comes from:** Oracle's documented "let users launch compute
instances" policy is `manage instance-family`, which includes deleting and
editing instances. The `where request.permission = …` conditions narrow it to
creation.

**UNVERIFIED:** that these conditions grant everything LaunchInstance needs
(it may also require a VNIC or boot-volume permission not listed). So the test
in §4 is mandatory. If a launch fails with NotAuthorized, add the single
permission named in the error, not the whole family.

## 3. API key, generated and kept on your Mac
```bash
brew install oci-cli
mkdir -p ~/.oci && chmod 700 ~/.oci
oci setup keys --key-name kalshi-retry --output-dir ~/.oci    # asks for a passphrase: set one
pbcopy < ~/.oci/kalshi-retry_public.pem                         # PUBLIC key only
```
- Console → Users → `kalshi-retry` → **API keys** → **Add API key** → **Paste
  public key**.
- The console then shows a config snippet. Save it to `~/.oci/config` under
  a profile `[KALSHI_RETRY]`, with `key_file=~/.oci/kalshi-retry.pem` and
  `pass_phrase` omitted (you'll be prompted).
- The private `.pem` never leaves `~/.oci`: not in the repo, not in a GitHub
  secret, not in a chat.

## 4. Prove it is restricted (mandatory, before the first retry)
```bash
export OCI_CLI_PROFILE=KALSHI_RETRY
oci iam compartment list --all >/dev/null && echo "read ok"
# Must FAIL with NotAuthorizedOrNotFound:
oci compute instance terminate --instance-id <any-instance-ocid> --force
oci iam user list    # must also fail or return nothing useful
```
If the terminate succeeds, the policy is too broad: delete the API key and fix
the policy before going further.

## 5. Run the retry
```bash
cd deploy/oracle/retry
./launch-retry.sh            # reads settings from retry.env (copy retry.env.example)
```
- Cycles AD-1 → AD-2 → AD-3 for **VM.Standard.A1.Flex 1 OCPU / 6 GB**, Ubuntu
  24.04 aarch64, 100 GB boot, your SSH **public** key.
- Waits 90 s between attempts, and stops at the first success.
- Prints the instance OCID and public IP, and logs every attempt to
  `retry.log`.
- Run it from a terminal you can leave open (or `caffeinate -i
  ./launch-retry.sh` so the Mac doesn't sleep). Stop it with Ctrl-C.

After the VM exists: **delete the API key** from `kalshi-retry` in the
console. The user has no further purpose.
