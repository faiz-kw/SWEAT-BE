from pathlib import Path
p=Path(r'C:\Users\Masheera\Downloads\VibeCopilot_Projects\New_folder\SWEAT-FE\src\routes\login.tsx')
s=p.read_text()
s=s.replace(' * Clean single-card UI requiring ONLY:', ' * Single login form with an organization code for tenant access:').replace(' * Automatic server-side identity directory routing.', ' * Blank organization code is reserved for platform superadmins.')
s=s.replace("import { BrandLogo } from '@/components/ui/BrandLogo';", "import { BrandLogo } from '@/components/ui/BrandLogo';\nimport { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle, AlertDialogDescription, AlertDialogFooter, AlertDialogAction } from '@/components/ui/alert-dialog';")
s=s.replace("  const [password, setPassword]", "  const [organizationCode, setOrganizationCode] = React.useState('');\n  const [acknowledgment, setAcknowledgment] = React.useState<string | null>(null);\n  const organizationInput = React.useRef<HTMLInputElement>(null);\n  const [password, setPassword]")
s=s.replace('        password,\n', '        password,\n        tenant_slug: organizationCode.trim().toLowerCase(),\n',1)
s=s.replace('        err?.response?.data?.error ||', '        err?.data?.error || err?.response?.data?.error ||')
s=s.replace('      setError(serverMsg);', "      if ((err?.data?.code || err?.response?.data?.code) === 'organization_required') {\n        setAcknowledgment(serverMsg);\n      } else {\n        setError(serverMsg);\n      }")
s=s.replace('            {/* Username or Email field */}', '''            <div className="space-y-1.5">
              <Label htmlFor="login-organization" className="text-xs font-semibold">
                Organization code
              </Label>
              <Input
                ref={organizationInput}
                id="login-organization"
                name="organization"
                autoComplete="organization"
                autoCapitalize="none"
                spellCheck={false}
                placeholder="Enter your gym's organization code"
                value={organizationCode}
                onChange={(e) => setOrganizationCode(e.target.value)}
                disabled={isSubmitting}
                aria-describedby="organization-help"
                className="h-10 text-[13.5px] rounded-lg bg-muted/30"
              />
              <p id="organization-help" className="text-xs text-muted-foreground">
                Required for gym accounts. Platform superadmins can leave this blank.
              </p>
            </div>
            {/* Username or Email field */}''')
s=s.replace('      {/* Background ambient lighting effects */}', '''      <AlertDialog open={acknowledgment !== null} onOpenChange={(open) => { if (!open) setAcknowledgment(null); }}>
        <AlertDialogContent onCloseAutoFocus={(event) => { event.preventDefault(); organizationInput.current?.focus(); }}>
          <AlertDialogHeader>
            <AlertDialogTitle>Organization code required</AlertDialogTitle>
            <AlertDialogDescription>{acknowledgment}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setAcknowledgment(null)}>OK</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {/* Background ambient lighting effects */}''')
p.write_text(s)
