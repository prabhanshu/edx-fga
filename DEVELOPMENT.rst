Developing on edx-fga
=====================

Setup (including devstack setup)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Install vagrant: http://docs.vagrantup.com/v2/installation/
#. Install virtualbox: https://www.virtualbox.org/wiki/Downloads
#. Set up devstack::

    mkdir devstack
    cd devstack
    curl -L https://raw.githubusercontent.com/edx/configuration/master/vagrant/release/devstack/Vagrantfile > Vagrantfile
    vagrant plugin install vagrant-vbguest
    vagrant up

#. Fork https://github.com/mitodl/edx-fga.git to your own github account.
#. Set up your development environment::

    cd themes/
    git clone https://github.com/your-name/edx-fga.git
    vagrant ssh    
    sudo su edxapp    
    cd ./themes/    
    pip uninstall edx-fga     (since it's part of the edx distribution, we have to remove the installed version)
    cd edx-fga/
    pip install -e .    
    paver run_all_servers    

You should now see your fork of the most recent master branch of edx-fga running in the LMS.

Developing
~~~~~~~~~~

#. In your host filesystem::

    cd /path/to/devstack/edx-platform/themes/edx-fga
    git branch feature/your-name/name-of-feature    

#. Write Code, then::

    git add .    
    git commit -m "Description of feature added."    
    git push origin feature/your-name/name-of-feature    

#. Rebase your branch against mitodl/master and resolve any conflicts, following this process: https://github.com/edx/edx-platform/wiki/How-to-Rebase-a-Pull-Request.
#. Open a pull request from your fork/feature branch to mitodl/master

Also, see testing: https://github.com/mitodl/edx-fga#testing.

