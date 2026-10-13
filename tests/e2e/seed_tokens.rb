# Run inside the GitLab container with `gitlab-rails runner`. Creates two root tokens with
# known values: an admin token for seeding, and one that has already expired. Idempotent.
ADMIN_TOKEN = ENV.fetch("E2E_ADMIN_TOKEN")
EXPIRED_TOKEN = ENV.fetch("E2E_EXPIRED_TOKEN")

root = User.find_by_username!("root")

def ensure_token(user, name, value, scopes, expires_at)
  token = PersonalAccessToken.find_by_token(value)
  unless token
    token = user.personal_access_tokens.build(name: name, scopes: scopes, expires_at: 30.days.from_now)
    token.organization_id = user.organization_id if token.respond_to?(:organization_id=) && user.respond_to?(:organization_id)
    token.set_token(value)
    token.save!
  end
  # Written directly because validations reject an expiry in the past.
  token.update_columns(expires_at: expires_at, revoked: false)
end

ensure_token(root, "e2e-admin", ADMIN_TOKEN, %w[api admin_mode], 30.days.from_now.to_date)
ensure_token(root, "e2e-expired", EXPIRED_TOKEN, %w[read_api], 2.days.ago.to_date)
puts "e2e tokens ready"
