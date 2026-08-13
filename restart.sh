sudo systemctl daemon-reload
sudo systemctl restart nexus-mailbox.timer
sudo systemctl start nexus-mailbox.service
sudo systemctl restart agent-api
sudo systemctl status agent-api --no-pager
