export const TOPICS = {
  registered: 'identity.user.profile.registered.v1',
  signIn: 'identity.staff.sign_in_completed.v1',
  suspended: 'identity.account.suspended.v1',
  notification: 'notification.dispatch.requested.high.v1',
};

// 'ignored.topic.from_comment.v1'
export const authorize = (token: any) => token.realm_access?.roles ?? [];
