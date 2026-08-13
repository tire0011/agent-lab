sudo systemctl start nexus-mailbox.service
journalctl -u nexus-mailbox.service -n 30 --no-pager
