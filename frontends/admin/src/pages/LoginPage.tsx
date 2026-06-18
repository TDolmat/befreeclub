import { useState } from 'react';
import { LayoutGrid, Loader2, LogIn } from 'lucide-react';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/core/components/ui/card';
import { Input } from '@/core/components/ui/input';
import { Label } from '@/core/components/ui/label';
import { useLogin } from '@/core/hooks/useAuth';
import { AuthError } from '@/core/lib/auth-api';

export function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const login = useLogin();

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);
    login.mutate(
      { email: email.trim(), password },
      {
        onError: (err) => {
          if (err instanceof AuthError) {
            setErrorMsg(err.message);
          } else {
            setErrorMsg('Nieoczekiwany błąd. Spróbuj jeszcze raz.');
          }
        },
      },
    );
  };

  return (
    <div className="min-h-screen bg-background text-foreground grid place-items-center px-4">
      <Card glow className="w-full max-w-md animate-fade-in">
        <CardHeader>
          <div className="flex items-center gap-3 mb-2">
            <div className="h-10 w-10 rounded-lg bg-primary/15 border border-primary/30 grid place-items-center">
              <LayoutGrid className="h-5 w-5 text-primary" />
            </div>
            <div>
              <CardTitle className="auth-title text-xl leading-tight">Be Free Club</CardTitle>
              <CardDescription className="text-xs uppercase tracking-widest">
                Panel administratora
              </CardDescription>
            </div>
          </div>
          <CardDescription>Zaloguj się żeby kontynuować.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="flex flex-col gap-3">
            <div>
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="email"
                autoFocus
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={login.isPending}
              />
            </div>
            <div>
              <Label htmlFor="password">Hasło</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={login.isPending}
              />
            </div>
            {errorMsg && (
              <p className="text-xs text-destructive bg-destructive/10 border border-destructive/30 rounded px-3 py-2">
                {errorMsg}
              </p>
            )}
            <Button
              type="submit"
              variant="default"
              className="mt-2 w-full"
              disabled={login.isPending || !email || !password}
            >
              {login.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <LogIn className="h-4 w-4" />
              )}
              Zaloguj
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
