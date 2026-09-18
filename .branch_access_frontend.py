from pathlib import Path
p=Path(r"C:\Users\Masheera\Downloads\VibeCopilot_Projects\New_folder\SWEAT-FE\src\components\admin\UsersWorkspace.tsx")
s=p.read_text(encoding="utf-8")
s=s.replace('    setEditStatus(user.status || (user.is_active ? "Active" : "Inactive"));', '    const status = String(user.status || (user.is_active ? "ACTIVE" : "INACTIVE")).toUpperCase();\n    setEditStatus(status === "ACTIVE" ? "Active" : status === "INVITED" ? "Invited" : status === "SUSPENDED" ? "Suspended" : "Inactive");')
s=s.replace('        status: editStatus === "Active" ? "ACTIVE" : editStatus === "Invited" ? "INVITED" : "INACTIVE",', '        ...(editStatus.toUpperCase() !== String(editingUser.status).toUpperCase() ? { status: editStatus.toUpperCase() } : {}),')
p.write_text(s,encoding="utf-8")
