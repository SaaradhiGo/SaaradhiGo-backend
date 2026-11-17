"""Deploy one commit to Railway PROD and wait for it (17 Nov; called only by CI on main).

    RAILWAY_PROD_TOKEN=... python .github/scripts/railway_deploy.py <commit sha> <service id> [<service id>...]

Services deploy in the order given, each waiting for SUCCESS before the next (the backend first, so
its pre-deploy migrations finish before a worker starts on the new code). The token is a GitHub
`production` environment secret; it is never printed.
"""
import json
import os
import sys
import time
import urllib.request

API = 'https://backboard.railway.com/graphql/v2'
ENVIRONMENT_ID = os.environ.get('RAILWAY_PROD_ENVIRONMENT_ID', 'cb9272bc-442c-4ebe-9292-15b48086144f')
DONE = {'SUCCESS'}
FAILED = {'FAILED', 'CRASHED', 'REMOVED', 'SKIPPED'}


def gql(query, variables):
    rq = urllib.request.Request(API, data=json.dumps({'query': query, 'variables': variables}).encode(), headers={
        'Content-Type': 'application/json', 'User-Agent': 'saaradhigo-ci',
        'Project-Access-Token': os.environ['RAILWAY_PROD_TOKEN']})
    with urllib.request.urlopen(rq, timeout=60) as r:
        out = json.loads(r.read())
    if out.get('errors'):
        raise SystemExit(f"Railway refused: {out['errors'][0].get('message')}")
    return out['data']


def deploy(sha, service):
    dep = gql('mutation($s:String!,$e:String!,$c:String!){ serviceInstanceDeployV2(serviceId:$s, environmentId:$e, commitSha:$c) }',
              {'s': service, 'e': ENVIRONMENT_ID, 'c': sha})['serviceInstanceDeployV2']
    print(f'service {service}: deployment {dep} for {sha[:8]}', flush=True)
    deadline = time.time() + 30 * 60
    status = ''
    while time.time() < deadline:
        status = gql('query($i:String!){ deployment(id:$i){ status } }', {'i': dep})['deployment']['status']
        if status in DONE:
            print(f'service {service}: {status}', flush=True)
            return
        if status in FAILED:
            raise SystemExit(f'service {service}: deployment {dep} ended {status}')
        time.sleep(15)
    raise SystemExit(f'service {service}: deployment {dep} still {status} after 30 minutes')


if __name__ == '__main__':
    commit, services = sys.argv[1], sys.argv[2:]
    for svc in services:
        deploy(commit, svc)
