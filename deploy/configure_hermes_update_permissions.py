import pathlib, subprocess
commands=[]
for unit in ('hermes-gateway','hermes-dashboard','hermes-pattern-delivery'):
    for spelling in (unit,unit+'.service'):
        for prefix in ('','--no-ask-password '):
            for verb in ('start','stop','restart','reset-failed'):
                commands.append('/usr/bin/systemctl '+prefix+verb+' '+spelling)
policy='hermes ALL=(root) NOPASSWD: '+', '.join(commands)+'\n'
staged=pathlib.Path('/opt/hermes/deploy/hermes-update-sudoers')
staged.write_text(policy);staged.chmod(0o440)
subprocess.run(['visudo','-c','-f',str(staged)],check=True)
target=pathlib.Path('/etc/sudoers.d/hermes-update')
target.write_text(policy);target.chmod(0o440)
for spelling in ('hermes-dashboard','hermes-dashboard.service'):
    subprocess.run(['runuser','-u','hermes','--','sudo','-n','-l','/usr/bin/systemctl','restart',spelling],check=True)
print('Updater permissions validated for both exact service-name spellings.')
