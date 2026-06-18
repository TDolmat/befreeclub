import { Link, useLocation } from 'react-router-dom';
import { Inbox, Settings, Users } from 'lucide-react';
import { Button } from '@/core/components/ui/button';
import { cn } from '@/core/lib/utils';

export function DmSubNav() {
  const location = useLocation();
  const path = location.pathname;
  const isAccounts = path.startsWith('/circle-dm/accounts');
  const isSettings = path.startsWith('/circle-dm/settings');
  const isInboxLike = !isAccounts && !isSettings;

  return (
    <>
      <Link to="/circle-dm">
        <Button
          variant={isInboxLike ? 'outline' : 'ghost'}
          size="sm"
          className={cn(isInboxLike && 'border-primary/40')}
        >
          <Inbox className="h-4 w-4" />
          Inbox
        </Button>
      </Link>
      <Link to="/circle-dm/accounts">
        <Button
          variant={isAccounts ? 'outline' : 'ghost'}
          size="sm"
          className={cn(isAccounts && 'border-primary/40')}
        >
          <Users className="h-4 w-4" />
          Konta
        </Button>
      </Link>
      <Link to="/circle-dm/settings">
        <Button
          variant={isSettings ? 'outline' : 'ghost'}
          size="icon"
          className={cn(isSettings && 'border-primary/40')}
          title="Ustawienia"
        >
          <Settings className="h-4 w-4" />
        </Button>
      </Link>
    </>
  );
}
