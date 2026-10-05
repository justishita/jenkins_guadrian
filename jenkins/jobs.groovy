// Job DSL script to configure Multibranch Pipeline for target-app
// Discovers branches and pull requests from GitHub repository

def targetOwner = System.getenv('GITHUB_REPO_OWNER') ?: (System.getenv('GITHUB_USERNAME') ?: 'justishita')
def targetRepo = System.getenv('GITHUB_REPO_NAME') ?: (System.getenv('GITHUB_REPO') ?: 'jenkins_guadrian')
def credId = System.getenv('GITHUB_CREDENTIALS_ID') ?: 'github-ssh'

multibranchPipelineJob('target-app') {
    displayName('Target Application Multibranch Pipeline')
    description('Automated CI/CD pipeline for target_app with automatic PR and branch discovery')

    branchSources {
        github {
            id('target-app-github-source')
            scanCredentialsId(credId)
            repoOwner(targetOwner)
            repository(targetRepo)
            buildOriginBranch(true)
            buildOriginPRMerge(true)
            buildOriginPRHead(false)
            buildForkPRMerge(true)
            buildForkPRHead(false)
        }
    }

    factory {
        workflowBranchProjectFactory {
            scriptPath('target_app/Jenkinsfile')
        }
    }

    orphanedItemStrategy {
        discardOldItems {
            numToKeep(20)
            daysToKeep(7)
        }
    }

    triggers {
        periodicFolderTrigger {
            interval('2m')
        }
    }
}
