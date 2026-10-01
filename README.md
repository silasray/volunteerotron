# Volunteer Scheduler

Two Flask apps:

- **`web/`** serves the pages. It never touches the database, and gets everything from the API.
- **`api/`** holds the logic and the JSON endpoints, and is the only part that talks to the database.

## Local development

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

.venv\Scripts\python run_api.py   # http://127.0.0.1:5001/api
.venv\Scripts\python run_web.py   # http://127.0.0.1:5000
```

Locally the API uses a SQLite file in `instance/` and creates any missing tables on startup. To create an admin user, run:

```powershell
.venv\Scripts\flask --app api create-user <name> --superuser   # prompts for the password
```

To use Postgres instead, set `DATABASE_URL`, for example `postgresql+psycopg://user:pass@host/db`. Then run migrations with `flask --app api db upgrade`, and set `AUTO_CREATE_TABLES=0`.

### Changing the schema

After editing `api/models.py`, generate a migration and review it before committing:

```powershell
.venv\Scripts\flask --app api db migrate -m "describe the change"
```

Generate migrations against Postgres. Autogenerating against SQLite produces Postgres-incompatible defaults.

## AWS deployment (Lambda + API Gateway + RDS)

```
browser ──HTTPS──> API Gateway (HTTP API) ──> WebFunction ──Lambda invoke──> ApiFunction ──> RDS Postgres
                                              (outside VPC)                 (private VPC)     (private VPC)
```

- **The API has no public route.** Only `WebFunction` can invoke it, which AWS permissions enforce. So the client IP the web forwards can be trusted for rate limiting.
- **No NAT gateway.** Nothing inside the VPC needs the internet.
- **All state is in Postgres:** data and rate-limit counters. Sessions are signed cookies and API tokens are signed, so both Lambdas are stateless.
- **`template.yaml`** defines all of this.

### Costs (very low traffic)

| Resource | Cost |
|---|---|
| RDS `db.t4g.micro` with 20 GB gp2 | Free for 12 months on a new account, then about $13–15 a month. This is most of the bill. |
| Secrets Manager: two app keys, plus the RDS master password | About $1.20 a month |
| Lambda, HTTP API, CloudWatch Logs | Cents a month at this traffic |

### Prerequisites

- **AWS CLI and SAM CLI,** signed in to your account.
- **No certificate needed.** For the custom domain, the stack creates and validates its own ACM certificate.
- **No Docker needed.** The build script downloads Linux arm64 wheels directly.

### First deploy

```powershell
.venv\Scripts\python scripts\build_lambda.py     # builds .build/lambda
sam deploy --guided                              # don't run "sam build"; the package is prebuilt
```

`--guided` asks for the stack name, region and parameters. None of them are secrets (see [Secrets](#secrets)):

- **`DbMasterUsername`:** keep the default. It's only used once, by `bootstrap-db`.
- **`AppKeysVersion`:** keep `1`.
- **`DomainName`:** `volunteerotron.silas.nyc`, or empty for the execute-api URL only.
- **`DelegationComplete`:** `false` for now. See [Custom domain](#custom-domain-volunteerotronsilasnyc) below.

The first deploy takes about 10 minutes, most of it creating RDS.

`samconfig.toml` is git-ignored. It only holds your deploy choices, no secrets.

### After the first deploy

Run these with the `ApiFunctionName` and `DatabaseMasterSecretArn` from the stack outputs. The payload files keep passwords out of your shell history.

```powershell
# 1. One time only: create the app's database login, scheduler_app (IAM auth, no password).
#    This is the only step that uses the RDS master password. It goes from Secrets Manager
#    straight into a payload file, which is deleted afterwards.
$m = aws secretsmanager get-secret-value --secret-id <DatabaseMasterSecretArn> --query SecretString --output text | ConvertFrom-Json
@{command = "bootstrap-db"; master_password = $m.password} | ConvertTo-Json -Compress | Set-Content -Encoding ascii bootstrap.json
Remove-Variable m
aws lambda invoke --function-name <ApiFunctionName> --cli-binary-format raw-in-base64-out `
    --payload file://bootstrap.json out.json; Remove-Item bootstrap.json; type out.json

# 2. Create the tables, as scheduler_app. (Payloads go in files: Windows PowerShell 5.1
#    strips the inner quotes from inline JSON passed to aws.)
'{"command": "migrate"}' | Set-Content -Encoding ascii migrate.json
aws lambda invoke --function-name <ApiFunctionName> --cli-binary-format raw-in-base64-out `
    --payload file://migrate.json out.json; type out.json

# first superuser: put {"command": "create-user", "name": "you", "password": "...", "superuser": true}
# in create-user.json, then:
aws lambda invoke --function-name <ApiFunctionName> --cli-binary-format raw-in-base64-out `
    --payload file://create-user.json out.json; type out.json; del create-user.json
```

## Custom domain: volunteerotron.silas.nyc

The stack creates its **own Route 53 hosted zone** for `volunteerotron.silas.nyc` and puts every record for the site there:
- the A alias to API Gateway
- ACM's validation record
- a CAA record

It never writes to the `silas.nyc` zone. Your only change there is **one NS record** that hands `volunteerotron` over to the new zone. Email, the blog and all other `silas.nyc` records are untouched.

The deploy runs in two passes. The certificate can only be validated once the NS record is live, and you only learn the nameservers to put in it after the first pass.

### Pass 1: create the hosted zone

Deploy with `DomainName=volunteerotron.silas.nyc` and `DelegationComplete=false`. The site works at the execute-api URL. From the stack outputs, note:

- **`SubdomainNameServers`:** four names like `ns-123.awsdns-45.com`, `ns-678.awsdns-90.net`, `ns-1234.awsdns-56.org` and `ns-2345.awsdns-67.co.uk`.

You can also see them in the Route 53 console. Go to **Hosted zones**, open **volunteerotron.silas.nyc** (the new zone, not `silas.nyc`), and look at its record of type **NS**.

### Before editing `silas.nyc`: check for conflicts

In the Route 53 console, open the **silas.nyc** hosted zone and filter the records by `volunteerotron`.

- **No records named `volunteerotron.silas.nyc`:** good, carry on.
- **A CNAME named `volunteerotron.silas.nyc`:** remove it first. Route 53 won't let a CNAME and an NS record share a name.
- **Other records at that name:** these will be hidden once the subdomain is delegated. Delete them if they were only placeholders.
- **A wildcard `*.silas.nyc` record:** leave it alone. After delegation, `volunteerotron` simply stops matching the wildcard. Every other name keeps working.

### Add the delegation (Route 53 console)

1. Open **Route 53**, then **Hosted zones**, then **silas.nyc**.
2. Click **Create record**. Don't edit the existing NS record.
   - The NS record named `silas.nyc`, with the zone's own nameservers, must stay exactly as it is.
   - The same goes for the SOA record.
   - You're adding a new NS record, at a different name.
3. Fill in:
   - **Record name:** `volunteerotron`. The console shows `.silas.nyc` after the box, so the full name becomes `volunteerotron.silas.nyc`.
   - **Record type:** `NS – Name servers for a hosted zone`.
   - **Value:** the four `SubdomainNameServers`, one per line. A trailing dot is optional.
   - **TTL (seconds):** `300`. Short while you confirm it works; you can raise it to `172800` (two days) later.
   - **Routing policy:** Simple routing.
4. Click **Create records**. The status shows *PENDING*, then *INSYNC* within about a minute.

#### Or with the AWS CLI

```powershell
$parent = (aws route53 list-hosted-zones-by-name --dns-name silas.nyc --max-items 1 --query "HostedZones[0].Id" --output text)
# Check that $parent is the silas.nyc zone, not volunteerotron's:
aws route53 get-hosted-zone --id $parent --query "HostedZone.Name"

# Put the four SubdomainNameServers values in ns.json:
@'
{"Comment": "Delegate volunteerotron.silas.nyc to its own hosted zone",
 "Changes": [{"Action": "CREATE", "ResourceRecordSet": {
   "Name": "volunteerotron.silas.nyc.", "Type": "NS", "TTL": 300,
   "ResourceRecords": [
     {"Value": "ns-XXXX.awsdns-XX.com."}, {"Value": "ns-XXXX.awsdns-XX.net."},
     {"Value": "ns-XXXX.awsdns-XX.org."}, {"Value": "ns-XXXX.awsdns-XX.co.uk."}]}}]}
'@ | Set-Content -Encoding ascii ns.json
aws route53 change-resource-record-sets --hosted-zone-id $parent --change-batch file://ns.json
```

`CREATE` fails rather than overwriting if a record with that name and type already exists.

### Check the delegation

```powershell
Resolve-DnsName volunteerotron.silas.nyc -Type NS -Server 8.8.8.8    # the four awsdns names above
Resolve-DnsName volunteerotron.silas.nyc -Type SOA -Server 8.8.8.8   # SOA from the new zone
Resolve-DnsName volunteerotron.silas.nyc -Type CAA -Server 8.8.8.8   # 0 issue "amazon.com"

# The rest of the domain is unchanged:
Resolve-DnsName silas.nyc -Type MX -Server 8.8.8.8
Resolve-DnsName silas.nyc -Type NS -Server 8.8.8.8                   # still the silas.nyc nameservers
```

If the first command still shows old answers, wait a few minutes. A resolver may have cached them, for up to the old TTL.

### Pass 2: turn the domain on

Build and run `sam deploy --guided` again. Accept the saved answers, but change **`DelegationComplete` to `true`**.

This pass:
- requests the certificate
- validates it through the new zone, usually within a few minutes
- creates the API Gateway custom domain and its alias record

The `SiteUrl` output then becomes `https://volunteerotron.silas.nyc/`.

### DNSSEC

If `silas.nyc` has DNSSEC signing turned on, leave it as it is. Delegating to an unsigned child zone works: with no DS record, the subdomain is simply unsigned. Only add a DS record for `volunteerotron` if you later enable DNSSEC signing on the new zone.

### Removing the site later

Do these in order, so the NS record never points at nameservers nobody controls:
1. Delete the `volunteerotron` NS record from the **silas.nyc** zone.
2. Delete the stack. The `volunteerotron.silas.nyc` hosted zone is deliberately kept.
3. Delete the leftover hosted zone in the console. You have to delete its records first, except NS and SOA.

## Secrets

None of these pass through deploy parameters or `samconfig.toml`, and none are in the code:

| Secret | Where it lives | Who can see it |
|---|---|---|
| RDS master password | Generated, stored and rotated by RDS in Secrets Manager (`DatabaseMasterSecretArn`) | Anyone with access to that secret. Only `bootstrap-db` uses it, once. |
| Database login for the app | None: `scheduler_app` has no password. The API Lambda signs a 15-minute IAM token for each new connection with its own role. | n/a |
| `ApiSecretKey` (signs API tokens) | Generated by CloudFormation in Secrets Manager; copied into the API Lambda's environment at deploy | Anyone who can read the secret or the API function's configuration |
| `WebSecretKey` (signs session cookies) | The same, for the web Lambda | Anyone who can read the secret or the web function's configuration |

**Why the app keys are still environment variables:** the API Lambda's private network has no route to Secrets Manager without a VPC endpoint, which costs about $7–15 a month. CloudFormation therefore reads the generated value at deploy time and sets it on the function. In practice, the people who can read them are whoever has `lambda:GetFunctionConfiguration` or `secretsmanager:GetSecretValue` in your account.

`ApiSecretKey` is the valuable one: anyone who has it can forge an API token for any user. `WebSecretKey` alone gets an attacker little, because admin actions also need a valid API token.

### Rotating an app key

```powershell
# New random value for one key (use ApiSecretKeySecret for the other); find the secret in the stack's resources
aws secretsmanager put-secret-value --secret-id <WebSecretKeySecret ARN> `
    --secret-string (aws secretsmanager get-random-password --password-length 64 --exclude-punctuation --query RandomPassword --output text)
# Then redeploy with AppKeysVersion bumped so the functions pick it up:
sam deploy --guided    # keep every saved answer, change only AppKeysVersion (e.g. 1 -> 2)
```

Use `--guided` rather than `--parameter-overrides AppKeysVersion=2`. Overrides given on the command line can replace your saved parameters instead of merging with them, which would reset `DomainName` and take the custom domain down.

When you rotate:
- **`WebSecretKey`:** everyone is signed out.
- **`ApiSecretKey`:** admins are signed out on their next click.

The RDS master password rotates by itself. Nothing in the app depends on it.

### Updating

```powershell
.venv\Scripts\python scripts\build_lambda.py
sam deploy
# if the release added migrations (migrate.json holds {"command": "migrate"}, as above):
aws lambda invoke --function-name <ApiFunctionName> --cli-binary-format raw-in-base64-out --payload file://migrate.json out.json
```

### Notes

- **Deleting the stack.** RDS has deletion protection, and a final snapshot is kept when the stack is deleted. Turn protection off in the console first if you really want it gone. For the custom domain's teardown order, see [Removing the site later](#removing-the-site-later).
- **Hosted zone cost.** The subdomain's hosted zone costs $0.50 a month.
- **Throttling.** API Gateway throttles the site at 20 requests per second, with bursts up to 50, which caps runaway costs. Raise it in `template.yaml` if real traffic needs more.
- **Cold starts.** After a period of no traffic, the first request is slower. The web and API functions each start cold, so it can take about 1–2 seconds.
