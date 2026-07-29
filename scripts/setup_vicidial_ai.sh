#!/usr/bin/env bash
# setup_vicidial_ai.sh — P5: create the VICIdial objects the AI agent needs.
#
# Creates, idempotently:
#   * an API user  (aiagent)         the agent authenticates as, at runtime
#   * a campaign   (AIOUT)           groups the calls
#   * a script     (AIOUT_PURPOSE)   why we are calling — the agent reads this, so
#                                    it stops inventing a pretext
#   * a list       (1001)            holds the leads
#   * three leads                    with lab-dialable phone numbers
#
# Run from the host:  bash scripts/setup_vicidial_ai.sh
set -euo pipefail

VM="${VM:-192.168.122.10}"
SSH=(ssh -o StrictHostKeyChecking=no -o BatchMode=yes "root@$VM")

# VICIdial's own DB account, from /etc/astguiclient.conf on the VM.
DBU="${DBU:-cron}" ; DBP="${DBP:-1234}" ; DB="${DB:-asterisk}"

API_USER="${API_USER:-aiagent}"
API_PASS="${API_PASS:-aiagent1234}"
CAMPAIGN="${CAMPAIGN:-AIOUT}"
SCRIPT_ID="${SCRIPT_ID:-AIOUTPURP}"
LIST_ID="${LIST_ID:-1001}"

# What the agent is calling about. This is the whole point of wiring the script
# in: left to itself the model invented "I called to check in on your account".
PURPOSE="${PURPOSE:-You are calling to confirm the delivery address for order 4471 and to ask whether Saturday or Sunday suits them better for delivery. Do not discuss pricing, and do not promise a specific delivery time.}"

# SQL-escape it: a single quote in the purpose text would otherwise terminate the
# string literal and break the insert.
PURPOSE_SQL="${PURPOSE//\'/\'\'}"

say() { printf '\n== %s ==\n' "$1"; }

mysql_run() { "${SSH[@]}" "mysql -u $DBU -p$DBP $DB -B" ; }

say "API user: $API_USER"
# NOTE on the two gotchas here:
#  1. These permission columns are ENUMs like enum('0','1'). Passing the INTEGER 1
#     selects the FIRST enum element, which is '0' — the opposite of intended.
#     They must be quoted strings.
#  2. The Non-Agent API gates on TWO independent layers: api_allowed_functions
#     AND per-function column checks. lead_search alone needs
#     vdc_agent_api_access='1', modify_leads IN('1'..'5') and user_level > 7.
mysql_run <<SQL
INSERT INTO vicidial_users
  (user, pass, full_name, user_level, user_group, active,
   api_only_user, vdc_agent_api_access, modify_leads, view_reports,
   api_allowed_functions)
VALUES
  ('$API_USER', '$API_PASS', 'AI Voice Agent (API)', 8, 'ADMIN', 'Y',
   '1', '1', '1', '1',
   ' lead_all_info ccc_lead_info lead_field_info update_lead lead_search update_log_entry add_lead ')
ON DUPLICATE KEY UPDATE
  pass=VALUES(pass), user_level=VALUES(user_level), active='Y',
  api_only_user='1', vdc_agent_api_access='1', modify_leads='1', view_reports='1',
  api_allowed_functions=VALUES(api_allowed_functions);
SELECT user, user_level, api_only_user, vdc_agent_api_access, modify_leads, active
  FROM vicidial_users WHERE user='$API_USER';
SQL

say "campaign $CAMPAIGN + script $SCRIPT_ID"
mysql_run <<SQL
INSERT INTO vicidial_scripts (script_id, script_name, script_comments, script_text, active, user_group)
VALUES ('$SCRIPT_ID', 'AI outbound call purpose',
        'Read by host_ai/ai_agent.py so the agent states a real reason for calling.',
        '$PURPOSE_SQL', 'Y', 'ADMIN')
ON DUPLICATE KEY UPDATE script_text=VALUES(script_text), active='Y';

INSERT INTO vicidial_campaigns (campaign_id, campaign_name, campaign_description, active, campaign_script)
VALUES ('$CAMPAIGN', 'AI Outbound', 'Outbound calls handled by the local AI voice agent', 'Y', '$SCRIPT_ID')
ON DUPLICATE KEY UPDATE campaign_name=VALUES(campaign_name), active='Y',
                        campaign_script=VALUES(campaign_script);

INSERT INTO vicidial_lists (list_id, list_name, campaign_id, active, list_description)
VALUES ('$LIST_ID', 'AI test leads', '$CAMPAIGN', 'Y', 'Leads whose numbers route to the test softphone')
ON DUPLICATE KEY UPDATE campaign_id=VALUES(campaign_id), active='Y';

SELECT campaign_id, campaign_name, active, campaign_script FROM vicidial_campaigns WHERE campaign_id='$CAMPAIGN';
SELECT list_id, list_name, campaign_id, active FROM vicidial_lists WHERE list_id='$LIST_ID';
SQL

say "dispositions the agent writes back"
# Without these, update_lead still accepts the status string but it means nothing
# in VICIdial's reports. Defining them makes AI calls show up like any other.
mysql_run <<SQL
INSERT INTO vicidial_statuses (status, status_name, selectable, human_answered, category, sale, dnc, customer_contact, not_interested, unworkable, scheduled_callback, completed)
VALUES ('AICOMP', 'AI call - conversation completed', 'Y', 'Y', 'UNDEFINED', 'N', 'N', 'Y', 'N', 'N', 'N', 'Y')
ON DUPLICATE KEY UPDATE status_name=VALUES(status_name);
INSERT INTO vicidial_statuses (status, status_name, selectable, human_answered, category, sale, dnc, customer_contact, not_interested, unworkable, scheduled_callback, completed)
VALUES ('AINOCO', 'AI call - answered, no conversation', 'Y', 'Y', 'UNDEFINED', 'N', 'N', 'N', 'N', 'N', 'N', 'Y')
ON DUPLICATE KEY UPDATE status_name=VALUES(status_name);
INSERT INTO vicidial_statuses (status, status_name, selectable, human_answered, category, sale, dnc, customer_contact, not_interested, unworkable, scheduled_callback, completed)
VALUES ('AIXFER', 'AI call - transferred to human', 'Y', 'Y', 'UNDEFINED', 'N', 'N', 'Y', 'N', 'N', 'N', 'N')
ON DUPLICATE KEY UPDATE status_name=VALUES(status_name);
SELECT status, status_name, human_answered, completed FROM vicidial_statuses WHERE status LIKE 'AI%';
SQL

say "leads in list $LIST_ID"
# Phone numbers are routed to the test softphone by the [ai-lead-dial] context in
# asterisk/extensions_ai_outbound.conf. In production these would be real numbers
# reaching a real carrier trunk.
mysql_run <<SQL
INSERT INTO vicidial_list (list_id, phone_number, first_name, last_name, status, comments, address1, city)
SELECT * FROM (SELECT '$LIST_ID','9001','Ayesha','Khan','NEW','Order 4471 awaiting delivery slot','12 Jinnah Road','Lahore') AS v
WHERE NOT EXISTS (SELECT 1 FROM vicidial_list WHERE list_id='$LIST_ID' AND phone_number='9001');
INSERT INTO vicidial_list (list_id, phone_number, first_name, last_name, status, comments, address1, city)
SELECT * FROM (SELECT '$LIST_ID','9002','Bilal','Ahmed','NEW','Order 4471 awaiting delivery slot','8 Mall Avenue','Karachi') AS v
WHERE NOT EXISTS (SELECT 1 FROM vicidial_list WHERE list_id='$LIST_ID' AND phone_number='9002');
INSERT INTO vicidial_list (list_id, phone_number, first_name, last_name, status, comments, address1, city)
SELECT * FROM (SELECT '$LIST_ID','9003','Sara','Iqbal','NEW','Order 4471 awaiting delivery slot','44 Blue Area','Islamabad') AS v
WHERE NOT EXISTS (SELECT 1 FROM vicidial_list WHERE list_id='$LIST_ID' AND phone_number='9003');

SELECT lead_id, list_id, phone_number, first_name, last_name, status FROM vicidial_list WHERE list_id='$LIST_ID';
SQL

say "verify the API works as $API_USER"
LEAD=$("${SSH[@]}" "mysql -u $DBU -p$DBP $DB -B -N -e \"SELECT MIN(lead_id) FROM vicidial_list WHERE list_id='$LIST_ID';\"")
curl -s -m 15 -G "http://$VM/vicidial/non_agent_api.php" \
  --data-urlencode "source=aisetup" --data-urlencode "user=$API_USER" \
  --data-urlencode "pass=$API_PASS" --data-urlencode "function=lead_all_info" \
  --data-urlencode "lead_id=$LEAD" | head -c 400
echo

say "done"
echo "campaign=$CAMPAIGN list=$LIST_ID api_user=$API_USER first_lead=$LEAD"
echo "Next: install asterisk/extensions_ai_outbound.conf (it carries [ai-lead-dial]),"
echo "      then: host_ai/outbound.py --tunnel --campaign $CAMPAIGN"
