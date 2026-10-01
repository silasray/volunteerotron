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

`--guided` asks for the stack name, region and parameters:

- **`WebSecretKey`, `ApiSecretKey`:** two different random strings, each 32 characters or more. For example: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- **`DbPassword`:** 16–64 letters and digits.
- **`DomainName`:** `volunteerotron.silas.nyc`, or empty for the execute-api URL only.
- **`DelegationComplete`:** `false` for now. See [Custom domain](#custom-domain-volunteerotronsilasnyc) below.

The first deploy takes about 10 minutes, most of it creating RDS.

`samconfig.toml` is git-ignored because `--guided` can save parameter values into it.

### After the first deploy

Run these with the `ApiFunctionName` from the stack outputs. The payload files keep the password out of your shell history.

```powershell
# create the tables
aws lambda invoke --function-name <ApiFunctionName> --cli-binary-format raw-in-base64-out `
    --payload '{"command": "migrate"}' out.json; type out.json

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

Build and run `sam deploy --guided` again. Accept the saved answers, but change **`DelegationComplete` to `true`**. If it asks for the secret parameters again, enter the **same values** as before:
- New `WebSecretKey` or `ApiSecretKey` values would sign everyone out.
- A new `DbPassword` would change the database password.

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

### Updating

```powershell
.venv\Scripts\python scripts\build_lambda.py
sam deploy
# if the release added migrations:
aws lambda invoke --function-name <ApiFunctionName> --cli-binary-format raw-in-base64-out --payload '{"command": "migrate"}' out.json
```

### Notes

- **Secrets in environment variables.** The secrets and the database password are function environment variables, visible to anyone with Lambda read access in your account. Secrets Manager would avoid that, but needs a VPC endpoint (about $7 a month). The template uses parameters to keep costs down.
- **Deleting the stack.** RDS has deletion protection, and a final snapshot is kept when the stack is deleted. Turn protection off in the console first if you really want it gone. For the custom domain's teardown order, see [Removing the site later](#removing-the-site-later).
- **Hosted zone cost.** The subdomain's hosted zone costs $0.50 a month.
- **Throttling.** API Gateway throttles the site at 20 requests per second, with bursts up to 50, which caps runaway costs. Raise it in `template.yaml` if real traffic needs more.
- **Cold starts.** After a period of no traffic, the first request is slower. The web and API functions each start cold, so it can take about 1–2 seconds.
